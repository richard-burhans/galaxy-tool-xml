#!/usr/bin/env python3
"""Sweep public Galaxy tool repositories through the galaxy-tool-xml API.

A maintainer QA tool. It runs the public API over every tool in a set of public
Galaxy tool repositories — galaxyproject/tools-iuc, the repositories it links to,
and other high-quality community repositories — and checks the library's
invariants on each:

* it must not **crash** on tool input;
* serialising the parsed tree must be **idempotent** (CDATA/comments preserved);
* the API must not **mutate** the ``ToolDocument`` tree;
* the typed ``model()`` must agree with the tree's root attributes;
* ``parse_tool().well_formed`` must agree with whether ``load_tool`` raises;
* a macro-free tool must validate the same under every ``macro_handling``;
* ``newest_valid_profile`` must return the newest profile that validates.

Each distinct violation is retained under ``tests/data/regressions/`` as a
permanent regression fixture. Validity contiguity is also measured, but a
non-contiguous tool is reported as a statistic, not a bug — it is an expected
real-world property that ``newest_valid_profile`` handles by design.

``--source`` picks which corpus to sweep. ``github`` (default) walks the
repositories listed in ``corpus_sources.json``, cloning any that are missing.
``toolshed`` walks ``corpus/galaxy-toolshed/`` which must be populated first
via ``scripts/fetch_toolshed.py``. ``combined`` walks both source trees and
deduplicates tools whose XML bytes are identical (sha256), producing the
canonical cross-source view. Each source writes to its own artifact
(``docs/corpus_stats.md`` for github, ``docs/toolshed_corpus_stats.md`` for
toolshed, ``docs/combined_corpus_stats.md`` for combined) so the three
views remain independent — the combined view does not auto-refresh the
per-source files.

The stats artifact summarises the distribution of declared profiles, newest
validating profiles, the cross-tab between them, macro usage, and validity
contiguity. The declared-profile distribution and cross-tab use the profile
**after macro expansion** (what Galaxy actually validates against); the raw
pre-expansion distribution is available via ``--include-raw-profile`` as a
diagnostic. The stats write is skipped for partial sweeps (``--limit`` or
``--repo``) and can be disabled with ``--no-stats``.

Usage::

    uv run python scripts/corpus_check.py [--source github|toolshed|combined] \
        [--repo NAME] [--limit N] [--no-stats] [--include-raw-profile]

GitHub-source repositories are shallow-cloned into the gitignored ``corpus/``
directory and reused on later runs. A repository that cannot be cloned is
skipped with a warning.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import traceback
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path

from lxml import etree

from galaxy_tool_xml.binding import (
    ToolXmlSyntaxError,
    load_tool,
    newest_valid_profile,
    parse_tool,
    validate_tool,
)
from galaxy_tool_xml.corrections import suggest_corrections
from galaxy_tool_xml.document import ToolDocument
from galaxy_tool_xml.macros import expand_from_path, has_macros
from galaxy_tool_xml.profiles import available_profiles

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CORPUS_ROOT = _REPO_ROOT / "corpus"
_TOOLSHED_ROOT = _CORPUS_ROOT / "galaxy-toolshed"
_REGRESSIONS = _REPO_ROOT / "tests" / "data" / "regressions"
_STATS_FILES = {
    "github": _REPO_ROOT / "docs" / "corpus_stats.md",
    "toolshed": _REPO_ROOT / "docs" / "toolshed_corpus_stats.md",
    "combined": _REPO_ROOT / "docs" / "combined_corpus_stats.md",
}
_SOURCES = ("github", "toolshed", "combined")
_COMBINED_SUB_SOURCES = ("github", "toolshed")
_PROFILE_NONE = "(none)"
_PROFILE_EXPANSION_FAILED = "(expansion failed)"


@dataclass
class ToolStats:
    """One tool's contribution to the corpus statistics.

    ``profile_raw`` is the literal ``profile`` attribute on the un-expanded
    tree — useful for diagnostics (it shows how often the corpus relies on
    macro tokens like ``@PROFILE@``). ``profile_expanded`` is the attribute
    after macros are expanded, which is what Galaxy actually validates
    against; this is the field that informs design decisions about profiles.
    """

    profile_raw: str  # literal attribute, or _PROFILE_NONE
    profile_expanded: str  # post-expansion, _PROFILE_NONE, or _PROFILE_EXPANSION_FAILED
    newest_valid: str  # newest validating profile, or _PROFILE_NONE
    has_macros: bool
    contiguous: bool


_CORPUS_SOURCES_FILE = _REPO_ROOT / "corpus_sources.json"


@cache
def _corpus_sources() -> tuple[tuple[str, str], ...]:
    """Return ``(name, url)`` pairs for every corpus repository.

    Loaded once from ``corpus_sources.json`` at the repo root — the canonical
    source for anything that walks the corpus, so adding or rerouting a
    repository is a config edit, not a code change. The order in the file is
    preserved (sweep order), and ``name`` is also the local clone directory
    name under ``corpus/``.
    """
    raw = json.loads(_CORPUS_SOURCES_FILE.read_text(encoding="utf-8"))
    return tuple((entry["name"], entry["url"]) for entry in raw["repositories"])


def _clone_repo(name: str, url: str) -> Path | None:
    """Shallow-clone a repository into the corpus, or reuse / skip it."""
    dest = _CORPUS_ROOT / name
    if dest.exists():
        print(f"using existing clone: {name}")
        return dest
    print(f"cloning {url} ...")
    result = subprocess.run(
        ["git", "clone", "--depth", "1", url, str(dest)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(
            f"  SKIPPED {name}: clone failed — {result.stderr.strip()}",
            file=sys.stderr,
        )
        return None
    return dest


def _corpus_commit(repo_dir: Path) -> str:
    """Return a repository checkout's commit SHA, or ``"unknown"``."""
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "unknown"


def _iter_sources(
    source: str, *, repo_filter: str | None
) -> Iterable[tuple[str, Path, str]]:
    """Yield ``(display_name, repo_dir, version_label)`` for each repository.

    ``github`` walks the repositories listed in ``corpus_sources.json``,
    cloning any that aren't already on disk and labelling each with its
    git commit SHA. ``toolshed`` walks the per-owner ``<owner>/<name>``
    layout that ``scripts/fetch_toolshed.py`` produces under
    ``corpus/galaxy-toolshed/`` and labels each repo as ``"latest"`` (each
    directory holds only the latest revision's files).
    """
    if source == "github":
        for name, url in _corpus_sources():
            if repo_filter is not None and name != repo_filter:
                continue
            repo_dir = _clone_repo(name, url)
            if repo_dir is None:
                continue
            yield name, repo_dir, _corpus_commit(repo_dir)
        return
    if source == "toolshed":
        if not _TOOLSHED_ROOT.exists():
            print(
                f"no toolshed corpus at "
                f"{_TOOLSHED_ROOT.relative_to(_REPO_ROOT)}; "
                "run scripts/fetch_toolshed.py first",
                file=sys.stderr,
            )
            return
        for owner_dir in sorted(_TOOLSHED_ROOT.iterdir()):
            if not owner_dir.is_dir():
                continue
            for repo_dir in sorted(owner_dir.iterdir()):
                if not repo_dir.is_dir():
                    continue
                yield f"{owner_dir.name}/{repo_dir.name}", repo_dir, "latest"
        return
    raise ValueError(f"unknown source: {source!r}")


# --- invariant checks -------------------------------------------------------
#
# Each check takes a parsed tool and returns ``("ok", "")`` when the invariant
# holds, or ``(category, detail)`` describing the first violation. They are
# imported by tests/test_regressions.py so a retained fixture is replayed
# through the very same battery.


def _check_immutable(document: ToolDocument) -> tuple[str, str]:
    """The document tree must survive model / correction / validation intact."""
    before = etree.tostring(document.tree)
    document.model()
    suggest_corrections(document)
    validate_tool(document)
    if etree.tostring(document.tree) != before:
        return "tree-mutated", "an API call mutated the document tree"
    return "ok", ""


def _check_roundtrip(document: ToolDocument) -> tuple[str, str]:
    """Serialising the document tree must be idempotent — CDATA/comments kept."""
    parser = etree.XMLParser(strip_cdata=False)
    once = etree.tostring(document.tree)
    twice = etree.tostring(etree.fromstring(once, parser).getroottree())
    if once != twice:
        return "roundtrip-unstable", "tree serialisation is not idempotent"
    return "ok", ""


def _check_model(document: ToolDocument) -> tuple[str, str]:
    """A root attribute present in the tree must match the bound model.

    An attribute absent from the tree is skipped: xsdata applies the schema
    default (``version`` defaults to ``1.0.0``), so the model legitimately
    differs from the tree there.
    """
    model = document.model()
    for attr in ("id", "name", "version"):
        tree_value = document.root.get(attr)
        if tree_value is None:
            continue
        model_value = getattr(model, attr, None)
        if tree_value != model_value:
            return "model-mismatch", (
                f"model.{attr}={model_value!r} but tree @{attr}={tree_value!r}"
            )
    return "ok", ""


def _check_parse_load_agree(path: Path) -> tuple[str, str]:
    """``parse_tool().well_formed`` must agree with whether ``load_tool`` raises."""
    well_formed = parse_tool(path).well_formed
    try:
        load_tool(path)
        raised = False
    except ToolXmlSyntaxError:
        raised = True
    if well_formed == raised:
        return "parse-load-disagree", (
            f"parse_tool.well_formed={well_formed} but load_tool "
            f"{'raised' if raised else 'succeeded'}"
        )
    return "ok", ""


def _check_macro_handling(path: Path, document: ToolDocument) -> tuple[str, str]:
    """A macro-free tool must validate identically under every macro_handling."""
    if has_macros(document.root):
        return "ok", ""
    results = {
        mode: validate_tool(path, macro_handling=mode).valid
        for mode in ("off", "expand", "strip")
    }
    if len(set(results.values())) != 1:
        return "macro-handling-divergence", (
            f"macro-free tool validates differently per mode: {results}"
        )
    return "ok", ""


def validity_vector(path: Path) -> list[bool]:
    """Return whether the tool validates against each vendored profile, oldest first."""
    return [
        validate_tool(path, profile=profile).valid for profile in available_profiles()
    ]


def _check_newest_valid_profile(path: Path, vector: list[bool]) -> tuple[str, str]:
    """``newest_valid_profile`` must return the newest profile that validates."""
    profiles = available_profiles()
    expected = next(
        (p for p, ok in zip(reversed(profiles), reversed(vector), strict=True) if ok),
        None,
    )
    actual = newest_valid_profile(path)
    if actual != expected:
        return "wrong-newest-profile", (
            f"newest_valid_profile returned {actual!r}; "
            f"the validity vector's newest is {expected!r}"
        )
    return "ok", ""


def is_contiguous(vector: list[bool]) -> bool:
    """Whether a tool's valid profiles form a single contiguous range of releases."""
    if not any(vector):
        return True
    first = vector.index(True)
    last = len(vector) - 1 - vector[::-1].index(True)
    return all(vector[first : last + 1])


def _post_expansion_profile(
    path: Path, document: ToolDocument, *, has_macros_flag: bool
) -> str:
    """Return the tool's ``profile`` attribute after macro expansion.

    For macro-free tools, expansion is a no-op and the raw attribute is
    returned. For macro-using tools, the tool is expanded from disk and the
    profile is read from the expanded tree; ``_PROFILE_EXPANSION_FAILED`` is
    returned when expansion fails (in which case the raw attribute is
    typically a macro token like ``@PROFILE@`` that conveys no version).
    """
    if not has_macros_flag:
        return document.root.get("profile") or _PROFILE_NONE
    expanded, _errors = expand_from_path(path)
    if expanded is None:
        return _PROFILE_EXPANSION_FAILED
    return expanded.getroot().get("profile") or _PROFILE_NONE


def _exercise(
    path: Path, *, collect_stats: bool = True
) -> tuple[str, str, str, ToolStats | None]:
    """Run the public API over one XML file and check every invariant.

    Returns ``(status, detail, signature, stats)`` where ``status`` is
    ``skip`` (not a ``<tool>`` file), ``ok``, ``crash`` (the library raised),
    or an invariant category naming the violated property. ``non-contiguous``
    is reported too — it is an expected real-world property, not a library
    bug. ``stats`` is the tool's contribution to the corpus statistics, or
    ``None`` for ``skip``/``crash`` cases, and also when ``collect_stats`` is
    false (the macro-expansion step needed for ``profile_expanded`` is then
    skipped).
    """
    try:
        document = parse_tool(path).document
        if document is None or document.root.tag != "tool":
            return "skip", "", "", None
        vector = validity_vector(path)
        profiles = available_profiles()
        newest_valid = next(
            (
                profile
                for profile, ok in zip(
                    reversed(profiles), reversed(vector), strict=True
                )
                if ok
            ),
            _PROFILE_NONE,
        )
        contiguous = is_contiguous(vector)
        if collect_stats:
            has_macros_flag = has_macros(document.root)
            stats: ToolStats | None = ToolStats(
                profile_raw=document.root.get("profile") or _PROFILE_NONE,
                profile_expanded=_post_expansion_profile(
                    path, document, has_macros_flag=has_macros_flag
                ),
                newest_valid=newest_valid,
                has_macros=has_macros_flag,
                contiguous=contiguous,
            )
        else:
            stats = None
        for category, detail in (
            _check_immutable(document),
            _check_roundtrip(document),
            _check_model(document),
            _check_parse_load_agree(path),
            _check_macro_handling(path, document),
            _check_newest_valid_profile(path, vector),
        ):
            if category != "ok":
                return category, detail, category, stats
        if not contiguous:
            run = "".join("1" if ok else "0" for ok in vector)
            return "non-contiguous", f"validity vector: {run}", "non-contiguous", stats
    except Exception as exc:  # diagnostic sweep: every crash is a finding
        return "crash", traceback.format_exc(), _signature(exc), None
    return "ok", "", "", stats


def _signature(exc: BaseException) -> str:
    """A short, dedup-friendly key for a crash: exception type + deepest frame."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return type(exc).__name__
    deepest = frames[-1]
    return f"{type(exc).__name__} @ {Path(deepest.filename).name}:{deepest.lineno}"


def _imported_macro_files(path: Path) -> list[Path]:
    """Return the macro files a tool ``<import>``s, resolved beside the tool."""
    tree = etree.parse(str(path), etree.XMLParser(recover=True))
    base = path.parent
    return [
        base / element.text.strip()
        for element in tree.iter("import")
        if element.text and element.text.strip()
    ]


def _retain(path: Path, repo: str) -> Path:
    """Copy an offending tool, and any macro files it imports, into the fixtures."""
    name = f"{repo}__{path.parent.name or path.stem}"
    dest = _REGRESSIONS / name
    suffix = 2
    while dest.exists():
        dest = _REGRESSIONS / f"{name}-{suffix}"
        suffix += 1
    dest.mkdir(parents=True)
    shutil.copy(path, dest / "tool.xml")
    for macro in _imported_macro_files(path):
        if macro.is_file():
            shutil.copy(macro, dest / macro.name)
    return dest


def _known_signatures() -> set[str]:
    """Signatures already recorded in the regression PROVENANCE.md.

    A finding whose signature is already retained is not retained again, so
    re-running the sweep never duplicates a fixture.
    """
    path = _REGRESSIONS / "PROVENANCE.md"
    if not path.exists():
        return set()
    known = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("- ") and " — " in line:
            known.add(line.rsplit(" — ", 1)[-1].strip())
    return known


def _append_provenance(retained: list[tuple[str, str, Path, str, str]]) -> None:
    """Append the newly retained fixtures to the regression PROVENANCE.md."""
    path = _REGRESSIONS / "PROVENANCE.md"
    existing = path.read_text(encoding="utf-8").rstrip() if path.exists() else ""
    new = [
        f"- `{fixture}` — {repo} `{rel}` @ `{commit[:12]}` — {signature}"
        for fixture, repo, rel, commit, signature in retained
    ]
    path.write_text(existing + "\n\n" + "\n".join(new) + "\n", encoding="utf-8")


def _profile_sort_key(profile: str) -> tuple[int, ...]:
    """Sort key: sentinels first, then numeric profiles oldest→newest.

    ``_PROFILE_NONE`` sorts before ``_PROFILE_EXPANSION_FAILED``; anything
    that isn't ``MAJOR.MINOR`` integers (or one of the sentinels) sorts after
    numeric profiles.
    """
    if profile == _PROFILE_NONE:
        return (0,)
    if profile == _PROFILE_EXPANSION_FAILED:
        return (0, 1)
    parts = profile.split(".")
    if all(part.isdigit() for part in parts):
        return (1, *(int(part) for part in parts))
    return (2,)


def _profile_sort_key_newest_first(profile: str) -> tuple[int, ...]:
    """Sort key: numeric profiles newest→oldest, sentinels last."""
    if profile == _PROFILE_NONE:
        return (2,)
    if profile == _PROFILE_EXPANSION_FAILED:
        return (2, 1)
    parts = profile.split(".")
    if all(part.isdigit() for part in parts):
        return (0, *(-int(part) for part in parts))
    return (1,)


def _bar(value: int, max_value: int, *, width: int = 30) -> str:
    """Render an ASCII histogram bar (length scaled to ``max_value``)."""
    if max_value == 0:
        return ""
    blocks = round(value / max_value * width)
    return "█" * blocks


def _format_distribution(title: str, counts: Counter[str], *, total: int) -> list[str]:
    """Render a profile distribution as a markdown table with histogram bars."""
    max_value = max(counts.values(), default=0)
    lines = [
        f"## {title}",
        "",
        "| Profile | Tools | % | Histogram |",
        "|---|---:|---:|---|",
    ]
    for profile in sorted(counts, key=_profile_sort_key):
        value = counts[profile]
        pct = value / total * 100 if total else 0
        lines.append(f"| {profile} | {value} | {pct:.1f}% | {_bar(value, max_value)} |")
    return lines


def _format_crosstab(crosstab: Counter[tuple[str, str]]) -> list[str]:
    """Render the declared × newest-valid cross-tab as a markdown table.

    Declared rows are oldest-first with ``_PROFILE_NONE`` first; newest-valid
    columns are newest-first with ``_PROFILE_NONE`` last (the worst case).
    """
    declared = sorted({d for d, _ in crosstab}, key=_profile_sort_key)
    newest = sorted({n for _, n in crosstab}, key=_profile_sort_key_newest_first)
    lines = [
        "## Declared (post-expansion) × newest-valid (cross-tab)",
        "",
        "Rows: declared profile *after macro expansion* (oldest first). "
        "Columns: newest validating profile (newest first). Read across a "
        "row to see where tools at a given declared profile actually end up.",
        "",
    ]
    lines.append("| declared \\\\ newest | " + " | ".join(newest) + " |")
    lines.append("|---|" + "|".join(["---:"] * len(newest)) + "|")
    for d in declared:
        row = [d, *(str(crosstab.get((d, n), 0)) for n in newest)]
        lines.append("| " + " | ".join(row) + " |")
    return lines


def _format_sources_table(unique: Counter[str], duplicates: Counter[str]) -> list[str]:
    """Render the combined-mode per-source breakdown as a markdown table."""
    lines = [
        "## Sources",
        "",
        "Per-source contribution to the deduplicated combined corpus. "
        "*Unique tools* are the ones whose sha256 hadn't been seen before "
        "(github is walked first, so a tool that exists in both sources is "
        "credited to github); *duplicates dropped* are tools whose bytes "
        "matched an earlier-seen tool from any source.",
        "",
        "| Source | Unique tools | Duplicates dropped |",
        "|---|---:|---:|",
    ]
    total_unique = 0
    total_duplicates = 0
    for source in _COMBINED_SUB_SOURCES:
        u = unique.get(source, 0)
        d = duplicates.get(source, 0)
        total_unique += u
        total_duplicates += d
        lines.append(f"| {source} | {u} | {d} |")
    lines.append(f"| **total** | **{total_unique}** | **{total_duplicates}** |")
    return lines


def _format_binary_table(
    title: str, true_label: str, false_label: str, counts: Counter[bool]
) -> list[str]:
    """Render a true/false counter as a 2-row markdown table."""
    true_count = counts.get(True, 0)
    false_count = counts.get(False, 0)
    total = true_count + false_count
    lines = [f"## {title}", "", "| | Tools | % |", "|---|---:|---:|"]
    if total == 0:
        lines.append("| _(no data)_ |  |  |")
        return lines
    true_pct = true_count / total * 100
    false_pct = false_count / total * 100
    lines.append(f"| {true_label} | {true_count} | {true_pct:.1f}% |")
    lines.append(f"| {false_label} | {false_count} | {false_pct:.1f}% |")
    return lines


def _write_stats(
    *,
    stats_file: Path,
    source: str,
    repos: list[tuple[str, str, int]],
    declared_raw_counts: Counter[str],
    declared_expanded_counts: Counter[str],
    newest_valid_counts: Counter[str],
    crosstab: Counter[tuple[str, str]],
    macro_counts: Counter[bool],
    contiguity_counts: Counter[bool],
    include_raw: bool,
    total: int,
    source_unique_counts: Counter[str],
    source_duplicate_counts: Counter[str],
) -> None:
    """Write the corpus statistics artifact for one source.

    The post-expansion declared-profile distribution is the default view;
    the raw (pre-expansion) view is added only when ``include_raw`` is set —
    it shows how often the corpus declares its profile via a macro token
    like ``@PROFILE@`` and is a diagnostic, not a design input. ``source``
    labels the artifact so a reader can tell at a glance whether they're
    looking at the github sweep, the toolshed sweep, or the combined view.
    In ``combined`` mode, ``source_unique_counts`` and
    ``source_duplicate_counts`` populate a per-source breakdown table; in
    single-source modes those are empty and the per-repo table is shown
    instead.
    """
    stats_file.parent.mkdir(parents=True, exist_ok=True)
    if source == "combined":
        header_para = (
            f"Generated by `scripts/corpus_check.py --source combined` on "
            f"{date.today().isoformat()}. Swept {total} unique tools across "
            f"{', '.join(_COMBINED_SUB_SOURCES)}, deduplicated by sha256 of "
            f"each tool's bytes."
        )
        regen_para = (
            "This file is regenerated by every full run of "
            "`corpus_check.py --source combined` unless `--no-stats` is "
            "given. The per-source artifacts (`corpus_stats.md`, "
            "`toolshed_corpus_stats.md`) are not refreshed by this run."
        )
    else:
        header_para = (
            f"Generated by `scripts/corpus_check.py --source {source}` on "
            f"{date.today().isoformat()}. Swept {total} tools across "
            f"{len(repos)} repositories."
        )
        regen_para = (
            "This file is regenerated by every full run of `corpus_check.py` "
            "for this source unless `--no-stats` is given; partial sweeps "
            "(`--limit` or `--repo`) do not regenerate it. Per-repo version "
            "labels make the snapshot reproducible."
        )
    lines: list[str] = [
        f"# Corpus statistics — {source}",
        "",
        header_para,
        "",
        regen_para,
        "",
    ]
    if source == "combined":
        lines.extend(
            _format_sources_table(source_unique_counts, source_duplicate_counts)
        )
    else:
        lines.append("## Repositories")
        lines.append("")
        lines.append("| Repository | Version | Tools |")
        lines.append("|---|---|---:|")
        for name, commit, count in sorted(repos):
            lines.append(f"| {name} | `{commit[:12]}` | {count} |")
    lines.append("")
    lines.extend(
        _format_distribution(
            "Declared profile distribution (post macro expansion)",
            declared_expanded_counts,
            total=total,
        )
    )
    lines.append("")
    if include_raw:
        lines.extend(
            _format_distribution(
                "Declared profile distribution (raw, pre-expansion)",
                declared_raw_counts,
                total=total,
            )
        )
        lines.append("")
    lines.extend(
        _format_distribution(
            "Newest valid profile distribution", newest_valid_counts, total=total
        )
    )
    lines.append("")
    lines.extend(_format_crosstab(crosstab))
    lines.append("")
    lines.extend(
        _format_binary_table("Macro usage", "Uses macros", "Macro-free", macro_counts)
    )
    lines.append("")
    lines.extend(
        _format_binary_table(
            "Validity-vector contiguity",
            "Contiguous valid range",
            "Non-contiguous",
            contiguity_counts,
        )
    )
    lines.append("")
    stats_file.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str]) -> int:
    """Sweep one corpus source, report findings, and retain regression fixtures."""
    parser = argparse.ArgumentParser(
        description=(
            "Sweep a Galaxy tool corpus source (github, toolshed, or "
            "combined) through galaxy-tool-xml."
        )
    )
    parser.add_argument(
        "--source",
        choices=_SOURCES,
        default="github",
        help=(
            "which corpus to sweep: 'github' (default) walks the "
            "repositories listed in corpus_sources.json, cloning any that "
            "are missing; 'toolshed' walks corpus/galaxy-toolshed/ which "
            "must be populated first via scripts/fetch_toolshed.py; "
            "'combined' walks both source trees and deduplicates by "
            "sha256 of each tool's bytes for the canonical cross-source view"
        ),
    )
    parser.add_argument(
        "--repo",
        help="sweep only this repository (by name); --source github only",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="stop after N tools total (0 sweeps everything)",
    )
    parser.add_argument(
        "--no-stats",
        action="store_true",
        help="don't regenerate the corpus stats artifact for the selected source",
    )
    parser.add_argument(
        "--include-raw-profile",
        action="store_true",
        help=(
            "also include a raw (pre-macro-expansion) profile distribution "
            "in the stats artifact, useful for diagnosing how often the "
            "corpus declares its profile via a macro token like @PROFILE@"
        ),
    )
    args = parser.parse_args(argv)

    if args.repo is not None and args.source != "github":
        print(
            f"--repo is only supported with --source github, not {args.source!r}",
            file=sys.stderr,
        )
        return 1
    if args.source == "github" and args.repo is not None:
        known_names = {name for name, _ in _corpus_sources()}
        if args.repo not in known_names:
            known = ", ".join(sorted(known_names))
            print(f"unknown --repo {args.repo!r}; known: {known}", file=sys.stderr)
            return 1

    _CORPUS_ROOT.mkdir(parents=True, exist_ok=True)
    stats_file = _STATS_FILES[args.source]

    # The stats artifact is only written for full sweeps, so skip the
    # per-tool stats work (one macro expansion each) in any other case.
    collect_stats = not (args.no_stats or args.limit or args.repo)
    combined = args.source == "combined"
    sources_to_walk = _COMBINED_SUB_SOURCES if combined else (args.source,)
    tools = 0
    signatures: Counter[str] = Counter()
    retained_signatures = _known_signatures()
    retained: list[tuple[str, str, Path, str, str]] = []
    repo_tool_counts: list[tuple[str, str, int]] = []
    declared_raw_counts: Counter[str] = Counter()
    declared_expanded_counts: Counter[str] = Counter()
    newest_valid_counts: Counter[str] = Counter()
    crosstab: Counter[tuple[str, str]] = Counter()
    macro_counts: Counter[bool] = Counter()
    contiguity_counts: Counter[bool] = Counter()
    # Combined-mode bookkeeping: dedup tools by sha256 of their bytes, and
    # track per-source kept / duplicate counts for the artifact's Sources
    # table. Empty in single-source modes.
    seen_hashes: set[str] = set()
    source_unique_counts: Counter[str] = Counter()
    source_duplicate_counts: Counter[str] = Counter()
    limit_reached = False
    for source_label in sources_to_walk:
        if limit_reached:
            break
        for display_name, repo_dir, version in _iter_sources(
            source_label, repo_filter=args.repo if not combined else None
        ):
            repo_tool_count = 0
            for path in sorted(repo_dir.rglob("*.xml")):
                if args.limit and tools >= args.limit:
                    limit_reached = True
                    break
                if not path.is_file():
                    continue  # broken symlink or non-regular file — not a tool
                if combined:
                    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                    if content_hash in seen_hashes:
                        source_duplicate_counts[source_label] += 1
                        continue
                    seen_hashes.add(content_hash)
                status, detail, signature, stats = _exercise(
                    path, collect_stats=collect_stats
                )
                if status == "skip":
                    continue
                tools += 1
                repo_tool_count += 1
                source_unique_counts[source_label] += 1
                if stats is not None:
                    declared_raw_counts[stats.profile_raw] += 1
                    declared_expanded_counts[stats.profile_expanded] += 1
                    newest_valid_counts[stats.newest_valid] += 1
                    crosstab[(stats.profile_expanded, stats.newest_valid)] += 1
                    macro_counts[stats.has_macros] += 1
                    contiguity_counts[stats.contiguous] += 1
                if tools % 500 == 0:
                    print(f"  ... {tools} tools", file=sys.stderr)
                if status == "ok":
                    continue
                signatures[signature] += 1
                if signature not in retained_signatures:
                    retained_signatures.add(signature)
                    # display_name may carry a '/' for toolshed (owner/name);
                    # sanitize so retained fixture directories stay flat.
                    dest = _retain(path, display_name.replace("/", "__"))
                    relative = path.relative_to(repo_dir)
                    retained.append(
                        (dest.name, display_name, relative, version, signature)
                    )
                    print(
                        f"\n{status.upper()}  [{display_name}] {signature}\n  "
                        f"{relative}\n  retained -> {dest}"
                    )
                    print("  " + detail.strip().replace("\n", "\n  "))
            repo_tool_counts.append((display_name, version, repo_tool_count))
            if limit_reached:
                break

    print(f"\n--- swept {tools} tools ---")
    for signature, count in signatures.most_common():
        print(f"  {count:6d}  {signature}")
    if "non-contiguous" in signatures:
        print(
            "  note: non-contiguous validity is an expected real-world property,"
            " not a library bug — newest_valid_profile handles it."
        )
    if retained:
        _append_provenance(retained)
        print(
            f"\nretained {len(retained)} new regression fixture(s) under {_REGRESSIONS}"
        )
    stats_path = stats_file.relative_to(_REPO_ROOT)
    if args.no_stats:
        pass
    elif args.limit or args.repo:
        print(
            f"\ncorpus stats not regenerated: partial sweep "
            f"(--limit or --repo). Run the full sweep to refresh {stats_path}."
        )
    else:
        _write_stats(
            stats_file=stats_file,
            source=args.source,
            repos=repo_tool_counts,
            declared_raw_counts=declared_raw_counts,
            declared_expanded_counts=declared_expanded_counts,
            newest_valid_counts=newest_valid_counts,
            crosstab=crosstab,
            macro_counts=macro_counts,
            contiguity_counts=contiguity_counts,
            include_raw=args.include_raw_profile,
            total=tools,
            source_unique_counts=source_unique_counts,
            source_duplicate_counts=source_duplicate_counts,
        )
        print(f"\ncorpus stats -> {stats_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
