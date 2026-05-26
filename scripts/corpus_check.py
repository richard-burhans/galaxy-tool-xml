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

A corpus statistics artifact (``docs/corpus_stats.md``) is regenerated on
every full sweep, summarising the distribution of declared profiles, newest
validating profiles, the cross-tab between them, macro usage, and validity
contiguity. The stats write is skipped for partial sweeps (``--limit`` or
``--repo``) and can be disabled with ``--no-stats``.

Usage::

    uv run python scripts/corpus_check.py [--repo NAME] [--limit N] [--no-stats]

Repositories are shallow-cloned into the gitignored ``corpus/`` directory and
reused on later runs. A repository that cannot be cloned is skipped with a
warning.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import traceback
from collections import Counter
from dataclasses import dataclass
from datetime import date
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
from galaxy_tool_xml.macros import has_macros
from galaxy_tool_xml.profiles import available_profiles

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CORPUS_ROOT = _REPO_ROOT / "corpus"
_REGRESSIONS = _REPO_ROOT / "tests" / "data" / "regressions"
_STATS_FILE = _REPO_ROOT / "docs" / "corpus_stats.md"
_PROFILE_NONE = "(none)"


@dataclass
class ToolStats:
    """One tool's contribution to the corpus statistics."""

    profile: str  # declared profile, or _PROFILE_NONE
    newest_valid: str  # newest validating profile, or _PROFILE_NONE
    has_macros: bool
    contiguous: bool


# galaxyproject/tools-iuc, every repository its README links under
# "Other repositories with Galaxy tools", and further high-quality, actively
# maintained, planemo-tested community tool repositories.
_REPOS: tuple[tuple[str, str], ...] = (
    ("tools-iuc", "https://github.com/galaxyproject/tools-iuc"),
    ("bgruening-galaxytools", "https://github.com/bgruening/galaxytools"),
    ("tools-devteam", "https://github.com/galaxyproject/tools-devteam"),
    ("galaxy_blast", "https://github.com/peterjc/galaxy_blast"),
    ("pico_galaxy", "https://github.com/peterjc/pico_galaxy"),
    ("galaxy_mira", "https://github.com/peterjc/galaxy_mira"),
    ("modENCODE-Galaxy", "https://github.com/modENCODE-DCC/Galaxy"),
    ("biopython-galaxy_packages", "https://github.com/biopython/galaxy_packages"),
    ("tools-galaxyp", "https://github.com/galaxyproteomics/tools-galaxyp"),
    ("tools-colibread", "https://github.com/genouest/tools-colibread"),
    ("galaxy-csg", "https://github.com/gregvonkuster/galaxy-csg"),
    ("earlham-galaxytools", "https://github.com/TGAC/earlham-galaxytools"),
    ("AAFC-MBB-Galaxy", "https://github.com/AAFC-MBB/Galaxy"),
    ("einonm-galaxy-tools", "https://gitlab.com/einonm/galaxy-tools"),
    ("phac-nml-galaxy_tools", "https://github.com/phac-nml/galaxy_tools"),
    ("RECETOX-galaxytools", "https://github.com/RECETOX/galaxytools"),
    (
        "tools-metabolomics",
        "https://github.com/workflow4metabolomics/tools-metabolomics",
    ),
    ("Galaxy-M", "https://github.com/Viant-Metabolomics/Galaxy-M"),
    # Further community tool repositories beyond the tools-iuc README list.
    ("tools-artbio", "https://github.com/ARTbio/tools-artbio"),
    ("tools-ecology", "https://github.com/galaxyecology/tools-ecology"),
    ("fls-galaxy-tools", "https://github.com/fls-bioinformatics-core/galaxy-tools"),
    ("larch-tools", "https://github.com/MaterialsGalaxy/larch-tools"),
)


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


def _exercise(path: Path) -> tuple[str, str, str, ToolStats | None]:
    """Run the public API over one XML file and check every invariant.

    Returns ``(status, detail, signature, stats)`` where ``status`` is
    ``skip`` (not a ``<tool>`` file), ``ok``, ``crash`` (the library raised),
    or an invariant category naming the violated property. ``non-contiguous``
    is reported too — it is an expected real-world property, not a library
    bug. ``stats`` is the tool's contribution to the corpus statistics, or
    ``None`` for ``skip``/``crash`` cases where it cannot be computed.
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
        stats = ToolStats(
            profile=document.root.get("profile") or _PROFILE_NONE,
            newest_valid=newest_valid,
            has_macros=has_macros(document.root),
            contiguous=contiguous,
        )
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
    """Sort key: ``_PROFILE_NONE`` first, then numeric profiles oldest→newest.

    Anything that isn't ``MAJOR.MINOR`` integers sorts after numeric profiles.
    """
    if profile == _PROFILE_NONE:
        return (0,)
    parts = profile.split(".")
    if all(part.isdigit() for part in parts):
        return (1, *(int(part) for part in parts))
    return (2,)


def _profile_sort_key_newest_first(profile: str) -> tuple[int, ...]:
    """Sort key: numeric profiles newest→oldest, ``_PROFILE_NONE`` last."""
    if profile == _PROFILE_NONE:
        return (2,)
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
        "## Declared × newest-valid (cross-tab)",
        "",
        "Rows: declared profile (oldest first). Columns: newest validating "
        "profile (newest first). Read across a row to see where tools at a "
        "given declared profile actually end up.",
        "",
    ]
    lines.append("| declared \\\\ newest | " + " | ".join(newest) + " |")
    lines.append("|---|" + "|".join(["---:"] * len(newest)) + "|")
    for d in declared:
        row = [d, *(str(crosstab.get((d, n), 0)) for n in newest)]
        lines.append("| " + " | ".join(row) + " |")
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
    repos: list[tuple[str, str, int]],
    declared_counts: Counter[str],
    newest_valid_counts: Counter[str],
    crosstab: Counter[tuple[str, str]],
    macro_counts: Counter[bool],
    contiguity_counts: Counter[bool],
    total: int,
) -> None:
    """Write the corpus statistics artifact."""
    _STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# Corpus statistics",
        "",
        f"Generated by `scripts/corpus_check.py` on {date.today().isoformat()}. "
        f"Swept {total} tools across {len(repos)} repositories.",
        "",
        "This file is regenerated by every full run of `corpus_check.py` "
        "unless `--no-stats` is given; partial sweeps (`--limit` or `--repo`) "
        "do not regenerate it. Per-repo commit SHAs make the snapshot "
        "reproducible.",
        "",
        "## Repositories",
        "",
        "| Repository | Commit | Tools |",
        "|---|---|---:|",
    ]
    for name, commit, count in sorted(repos):
        lines.append(f"| {name} | `{commit[:12]}` | {count} |")
    lines.append("")
    lines.extend(
        _format_distribution(
            "Declared profile distribution", declared_counts, total=total
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
    _STATS_FILE.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str]) -> int:
    """Sweep the corpora, report findings, and retain new regression fixtures."""
    parser = argparse.ArgumentParser(
        description="Sweep public Galaxy tool repositories through galaxy-tool-xml."
    )
    parser.add_argument("--repo", help="sweep only this repository (by name)")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="stop after N tools total (0 sweeps everything)",
    )
    parser.add_argument(
        "--no-stats",
        action="store_true",
        help=(
            "don't regenerate the corpus stats artifact "
            f"({_STATS_FILE.relative_to(_REPO_ROOT)})"
        ),
    )
    args = parser.parse_args(argv)

    repos = [(n, u) for n, u in _REPOS if args.repo is None or n == args.repo]
    if not repos:
        known = ", ".join(name for name, _ in _REPOS)
        print(f"unknown --repo {args.repo!r}; known: {known}", file=sys.stderr)
        return 1
    _CORPUS_ROOT.mkdir(parents=True, exist_ok=True)

    tools = 0
    signatures: Counter[str] = Counter()
    retained_signatures = _known_signatures()
    retained: list[tuple[str, str, Path, str, str]] = []
    repo_tool_counts: list[tuple[str, str, int]] = []
    declared_counts: Counter[str] = Counter()
    newest_valid_counts: Counter[str] = Counter()
    crosstab: Counter[tuple[str, str]] = Counter()
    macro_counts: Counter[bool] = Counter()
    contiguity_counts: Counter[bool] = Counter()
    for name, url in repos:
        repo_dir = _clone_repo(name, url)
        if repo_dir is None:
            continue
        commit = _corpus_commit(repo_dir)
        repo_tool_count = 0
        for path in sorted(repo_dir.rglob("*.xml")):
            if args.limit and tools >= args.limit:
                break
            if not path.is_file():
                continue  # broken symlink or non-regular file — not a tool
            status, detail, signature, stats = _exercise(path)
            if status == "skip":
                continue
            tools += 1
            repo_tool_count += 1
            if stats is not None:
                declared_counts[stats.profile] += 1
                newest_valid_counts[stats.newest_valid] += 1
                crosstab[(stats.profile, stats.newest_valid)] += 1
                macro_counts[stats.has_macros] += 1
                contiguity_counts[stats.contiguous] += 1
            if tools % 500 == 0:
                print(f"  ... {tools} tools", file=sys.stderr)
            if status == "ok":
                continue
            signatures[signature] += 1
            if signature not in retained_signatures:
                retained_signatures.add(signature)
                dest = _retain(path, name)
                relative = path.relative_to(repo_dir)
                retained.append((dest.name, name, relative, commit, signature))
                print(
                    f"\n{status.upper()}  [{name}] {signature}\n  {relative}\n"
                    f"  retained -> {dest}"
                )
                print("  " + detail.strip().replace("\n", "\n  "))
        repo_tool_counts.append((name, commit, repo_tool_count))
        if args.limit and tools >= args.limit:
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
    stats_path = _STATS_FILE.relative_to(_REPO_ROOT)
    if args.no_stats:
        pass
    elif args.limit or args.repo:
        print(
            f"\ncorpus stats not regenerated: partial sweep "
            f"(--limit or --repo). Run the full sweep to refresh {stats_path}."
        )
    else:
        _write_stats(
            repos=repo_tool_counts,
            declared_counts=declared_counts,
            newest_valid_counts=newest_valid_counts,
            crosstab=crosstab,
            macro_counts=macro_counts,
            contiguity_counts=contiguity_counts,
            total=tools,
        )
        print(f"\ncorpus stats -> {stats_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
