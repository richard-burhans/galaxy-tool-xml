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
diagnostic. The **combined** artifact additionally breaks down the two
failure modes — macro-expansion failures and no-valid-profile schema errors —
into named reason categories so the "are these our bugs?" question can be
answered at a glance. The stats write is skipped for partial sweeps
(``--limit`` or ``--repo``) and can be disabled with ``--no-stats``.

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
import logging
import re
import shutil
import subprocess
import sys
import traceback
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
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
from galaxy_tool_xml.macros import MacroError, expand_from_path, has_macros
from galaxy_tool_xml.profiles import available_profiles, latest_profile

logger = logging.getLogger("corpus_check")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CORPUS_ROOT = _REPO_ROOT / "corpus"
_TOOLSHED_ROOT = _CORPUS_ROOT / "galaxy-toolshed"
_TOOLSHED_MANIFEST = _TOOLSHED_ROOT / "manifest.json"
_REGRESSIONS = _REPO_ROOT / "tests" / "data" / "regressions"
_STATS_FILES = {
    "github": _REPO_ROOT / "docs" / "corpus_stats.md",
    "toolshed": _REPO_ROOT / "docs" / "toolshed_corpus_stats.md",
    "combined": _REPO_ROOT / "docs" / "combined_corpus_stats.md",
}
_CORPUS_DATA_DIR = _REPO_ROOT / "docs" / "corpus_data"
_CORPUS_DATA_BASENAMES = {
    "github": "corpus_data",
    "toolshed": "toolshed_corpus_data",
    "combined": "combined_corpus_data",
}
_FINE_GRAINED_BASE_COLUMNS = ("repo", "version", "path", "tool_id", "sha256")
_FINE_GRAINED_PROFILE_COLUMNS = (
    "profile_raw",
    "profile_expanded",
    "newest_valid",
    "expansion_failure_reason",
    "no_valid_reason",
)
_FAILURE_DETAILS_SUBDIR = "failures"
# The toolshed's hgweb routes (`/repos/<o>/<n>/file/<rev>/...`) all return
# 403 Forbidden behind nginx; only the `/view/<owner>/<name>` UI is
# publicly reachable, so failure-page links land on the repo's browse
# page rather than a deep link to the specific file at the specific
# changeset. The path + version columns in the table tell the user
# what to navigate to once they're there.
_TOOLSHED_VIEW_URL = "https://toolshed.g2.bx.psu.edu/view"
_SOURCES = ("github", "toolshed", "combined")
_COMBINED_SUB_SOURCES = ("github", "toolshed")
_PROFILE_NONE = "(none)"
_PROFILE_EXPANSION_FAILED = "(expansion failed)"
_UNKNOWN = "unknown"


@dataclass
class ToolStats:
    """One tool's contribution to the corpus statistics.

    ``profile_raw`` is the literal ``profile`` attribute on the un-expanded
    tree — useful for diagnostics (it shows how often the corpus relies on
    macro tokens like ``@PROFILE@``). ``profile_expanded`` is the attribute
    after macros are expanded, which is what Galaxy actually validates
    against; this is the field that informs design decisions about profiles.
    ``tool_id`` is the ``@id`` attribute after macro expansion, falling back
    to the raw ``@id`` (or the empty string in the rare case it's missing) —
    it is the tool's logical identity, distinct from its file path.
    """

    profile_raw: str  # literal attribute, or _PROFILE_NONE
    profile_expanded: str  # post-expansion, _PROFILE_NONE, or _PROFILE_EXPANSION_FAILED
    tool_id: str  # post-expansion @id, or raw @id on expansion failure
    newest_valid: str  # newest validating profile, or _PROFILE_NONE
    validity: list[bool]  # one entry per available_profiles(), oldest first
    has_macros: bool
    contiguous: bool
    # Set only when profile_expanded == _PROFILE_EXPANSION_FAILED; categorises
    # the macro-expansion error (undefined macro, missing import, malformed
    # XML, etc.) — surfaced in the combined stats markdown.
    expansion_failure_reason: str | None = None
    # Set only when newest_valid == _PROFILE_NONE; categorises why no vendored
    # profile accepted the tool (XSD missing attribute, missing element,
    # invalid boolean, etc., or "(macro expansion failed)" when group A).
    no_valid_reason: str | None = None


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
        logger.info("using existing clone: %s", name)
        return dest
    logger.info("cloning %s ...", url)
    result = subprocess.run(
        ["git", "clone", "--depth", "1", url, str(dest)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.warning("SKIPPED %s: clone failed — %s", name, result.stderr.strip())
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


def _iter_github_sources(
    *,
    repo_filter: str | None,
) -> Iterable[tuple[str, str, Path, str]]:
    """Yield ``("github", display_name, repo_dir, commit_sha)`` per github repo.

    Walks the entries in ``corpus_sources.json``, cloning any that aren't
    already on disk and labelling each with its git commit SHA.
    """
    for name, url in _corpus_sources():
        if repo_filter is not None and name != repo_filter:
            continue
        repo_dir = _clone_repo(name, url)
        if repo_dir is None:
            continue
        yield "github", name, repo_dir, _corpus_commit(repo_dir)


@cache
def _toolshed_manifest() -> dict[str, str]:
    """Return ``owner/name -> short changeset`` from the toolshed manifest.

    ``scripts/fetch_toolshed.py`` writes ``manifest.json`` next to the
    per-owner clones with the tip changeset captured before ``.hg/`` is
    removed. An older corpus fetched before that change has no manifest;
    we log one warning, return ``{}``, and ``_iter_toolshed_sources``
    falls back to ``_UNKNOWN`` per repo so the sweep still runs.
    """
    if not _TOOLSHED_MANIFEST.exists():
        logger.warning(
            "no toolshed manifest at %s; toolshed versions will be %r. "
            "Re-run scripts/fetch_toolshed.py to populate it.",
            _TOOLSHED_MANIFEST.relative_to(_REPO_ROOT),
            _UNKNOWN,
        )
        return {}
    raw = json.loads(_TOOLSHED_MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {}
    repos = raw.get("repositories")
    if not isinstance(repos, dict):
        return {}
    result: dict[str, str] = {}
    for key, entry in repos.items():
        if not isinstance(entry, dict):
            continue
        changeset = entry.get("changeset")
        if isinstance(changeset, str) and changeset:
            result[key] = changeset
    return result


def _iter_toolshed_sources() -> Iterable[tuple[str, str, Path, str]]:
    """Yield ``("toolshed", "<owner>/<name>", repo_dir, changeset)`` per repo.

    Walks the per-owner ``<owner>/<name>`` layout that
    ``scripts/fetch_toolshed.py`` produces under ``corpus/galaxy-toolshed/``;
    each directory holds only the latest revision's files. The changeset
    comes from the toolshed manifest (``_toolshed_manifest``) and falls
    back to ``_UNKNOWN`` for clones fetched before the manifest existed.
    Callers must LBYL-check ``_TOOLSHED_ROOT.exists()`` before iterating
    — ``iterdir`` would otherwise raise ``FileNotFoundError`` and
    "missing corpus" would look the same as "empty corpus".
    """
    manifest = _toolshed_manifest()
    for owner_dir in sorted(_TOOLSHED_ROOT.iterdir()):
        if not owner_dir.is_dir():
            continue
        for repo_dir in sorted(owner_dir.iterdir()):
            if not repo_dir.is_dir():
                continue
            key = f"{owner_dir.name}/{repo_dir.name}"
            yield "toolshed", key, repo_dir, manifest.get(key, _UNKNOWN)


def _iter_sources(
    sources: tuple[str, ...],
    *,
    repo_filter: str | None,
) -> Iterable[tuple[str, str, Path, str]]:
    """Yield ``(source_label, display_name, repo_dir, version)`` for each repo.

    Dispatches per source name (``github`` or ``toolshed``) and chains the
    streams, so the caller iterates one flat sequence regardless of how many
    sources are walked. Combined-mode callers pass both source names.
    """
    for source in sources:
        if source == "github":
            yield from _iter_github_sources(repo_filter=repo_filter)
        elif source == "toolshed":
            yield from _iter_toolshed_sources()
        else:
            raise ValueError(f"unknown source: {source!r}")


# --- invariant checks -------------------------------------------------------
#
# Each check takes a parsed tool and returns ``("ok", "")`` when the invariant
# holds, or ``(category, detail)`` describing the first violation. They are
# imported by tests/test_regressions.py so a retained fixture is replayed
# through the very same battery.


def check_immutable(document: ToolDocument) -> tuple[str, str]:
    """The document tree must survive model / correction / validation intact."""
    before = etree.tostring(document.tree)
    document.model()
    suggest_corrections(document)
    validate_tool(document)
    if etree.tostring(document.tree) != before:
        return "tree-mutated", "an API call mutated the document tree"
    return "ok", ""


def check_roundtrip(document: ToolDocument) -> tuple[str, str]:
    """Serialising the document tree must be idempotent — CDATA/comments kept.

    The re-parse uses ``recover=True`` to match the initial parse: real-world
    tools occasionally violate the XML spec (e.g., ``--`` inside a comment)
    in ways lxml's recovery accepts. The meaningful check is therefore that
    the recovered tree re-serialises and re-recovers to the same bytes, not
    that a strict re-parse succeeds.
    """
    parser = etree.XMLParser(strip_cdata=False, recover=True)
    once = etree.tostring(document.tree)
    twice = etree.tostring(etree.fromstring(once, parser).getroottree())
    if once != twice:
        return "roundtrip-unstable", "tree serialisation is not idempotent"
    return "ok", ""


def check_model(document: ToolDocument) -> tuple[str, str]:
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


def check_parse_load_agree(path: Path) -> tuple[str, str]:
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


def check_macro_handling(path: Path, document: ToolDocument) -> tuple[str, str]:
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


def check_newest_valid_profile(path: Path, vector: list[bool]) -> tuple[str, str]:
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


_MACRO_FAIL_MALFORMED = re.compile(
    r"invalid element|tag mismatch|hyphen within comment|StartTag|EndTag"
)


def _expansion_failure_reason(errors: list[MacroError]) -> str:
    """Categorise the first ``MacroError`` from a failed expansion.

    Empirically the corpus produces three macro-failure modes plus a
    catch-all: undefined `<expand>` target, missing imported macro file,
    and malformed XML in the tool itself. The combined stats markdown
    aggregates these so the breakdown is visible at a glance.
    """
    if not errors:
        return "other macro error"
    message = errors[0].message
    if "No macro named" in message:
        return "undefined macro reference in <expand>"
    if "No such file or directory" in message:
        return "imported macros.xml file not on disk"
    if _MACRO_FAIL_MALFORMED.search(message):
        return "malformed XML in tool file"
    return "other macro error"


def _expanded_attrs(
    path: Path, document: ToolDocument, *, has_macros_flag: bool
) -> tuple[str, str, str | None]:
    """Return ``(profile, tool_id, expansion_failure_reason)`` after expansion.

    For macro-free tools, expansion is a no-op and the raw attributes are
    returned with ``expansion_failure_reason=None``. For macro-using tools
    the tool is expanded from disk **once** and both attributes are read
    from the expanded tree, so adding ``tool_id`` here costs no extra
    expansion. ``profile`` becomes ``_PROFILE_EXPANSION_FAILED`` when
    expansion fails; ``tool_id`` then falls back to the raw ``@id``
    literal (which may or may not contain a macro token like
    ``bcftools_@EXECUTABLE@`` — empirically 0 of 17 expansion-failed
    tools carried one in the 2026-05-27 combined sweep, but the
    fallback handles either case; see ``docs/decisions.md`` §10.8).
    ``expansion_failure_reason`` categorises the failure for the
    combined stats artifact.
    """
    raw_id = document.root.get("id") or ""
    if not has_macros_flag:
        return document.root.get("profile") or _PROFILE_NONE, raw_id, None
    expanded, errors = expand_from_path(path)
    if expanded is None:
        return _PROFILE_EXPANSION_FAILED, raw_id, _expansion_failure_reason(errors)
    expanded_root = expanded.getroot()
    return (
        expanded_root.get("profile") or _PROFILE_NONE,
        expanded_root.get("id") or raw_id,
        None,
    )


def _no_valid_reason(
    path: Path, document: ToolDocument, *, expansion_failure_reason: str | None
) -> str:
    """Categorise why a tool's validity vector is empty (no vendored profile).

    Returns the short ``"(macro expansion failed)"`` sentinel when the
    expansion never produced a tree (the per-reason breakdown is reported
    separately by the macro-expansion section). Otherwise runs one more
    ``validate_tool`` (a fresh parse + macro expansion + XSD validation)
    against the tool's declared profile to pull the first error and
    categorise it. The extra call only fires for no-valid tools (~8% of
    the combined sweep), so its cost is bounded and dwarfed by the
    sweep-wide validation work.
    """
    if expansion_failure_reason is not None:
        return "(macro expansion failed)"
    declared = document.root.get("profile")
    profile = declared if declared else latest_profile()
    result = validate_tool(path, profile=profile, on_missing="nearest")
    if result.syntax_errors:
        message = result.syntax_errors[0].message
        lowered = message.lower()
        if "character encoding" in lowered or "invalid bytes" in lowered:
            return "invalid character encoding (non-UTF-8 bytes)"
        return "other XML syntax error"
    if not result.errors:
        # validity_vector saw every profile fail yet this single probe
        # reports no schema errors; capture as untriaged rather than
        # silently dropping the count.
        return "untriaged (no schema error at probed profile)"
    message = result.errors[0].message
    if "is not allowed" in message and "attribute" in message:
        return "XSD does not declare attribute used by tool"
    if "not expected" in message and "Element" in message:
        return "XSD does not allow element under this parent"
    if "is not allowed" in message and "Element" in message:
        return "XSD does not allow element at all"
    if "required but missing" in message:
        return "XSD-required attribute missing on tool element"
    if "facet 'enumeration'" in message:
        return "attribute value outside XSD's enumeration"
    if "not a valid value" in message and (
        "boolean" in message.lower() or "PermissiveBoolean" in message
    ):
        return "invalid boolean ('True'/'False' vs 'true'/'false')"
    if "not a valid value" in message or "facet" in message:
        return "other XSD type / pattern mismatch"
    return "other"


@dataclass
class _SweepState:
    """Mutable bookkeeping shared across one ``main`` invocation's path loop.

    Bundling the dozen-plus counters / sets / lists into a single struct
    keeps ``_process_path`` to one ``state`` parameter (so the inner loop
    body in ``main`` stays shallow) and makes the dependency surface
    explicit instead of an implicit set of closures.
    """

    seen_hashes: set[str] = field(default_factory=set)
    sha_to_stats: dict[str, ToolStats] = field(default_factory=dict)
    rows: list[dict[str, str | int]] = field(default_factory=list)
    declared_raw_counts: Counter[str] = field(default_factory=Counter)
    declared_expanded_counts: Counter[str] = field(default_factory=Counter)
    newest_valid_counts: Counter[str] = field(default_factory=Counter)
    crosstab: Counter[tuple[str, str]] = field(default_factory=Counter)
    macro_counts: Counter[bool] = field(default_factory=Counter)
    contiguity_counts: Counter[bool] = field(default_factory=Counter)
    source_unique_counts: Counter[str] = field(default_factory=Counter)
    source_duplicate_counts: Counter[str] = field(default_factory=Counter)
    signatures: Counter[str] = field(default_factory=Counter)
    retained_signatures: set[str] = field(default_factory=set)
    retained: list[tuple[str, str, Path, str, str]] = field(default_factory=list)
    # Per-tool failure-reason counters surfaced in the combined stats markdown.
    expansion_failure_counts: Counter[str] = field(default_factory=Counter)
    no_valid_counts: Counter[str] = field(default_factory=Counter)


def _validity_column(profile: str) -> str:
    """Column name for the per-profile validity flag (e.g. ``valid_26.1``)."""
    return f"valid_{profile}"


def _make_row(
    *,
    display_name: str,
    version: str,
    path: Path,
    repo_dir: Path,
    sha: str,
    stats: ToolStats,
) -> dict[str, str | int | None]:
    """Construct one fine-grained data row from a tool's stats.

    Every row carries the full combined-schema columns (identifying
    fields, the profile fields, the two failure-reason fields, and one
    ``valid_<profile>`` flag per vendored profile); the per-source
    emitter drops the profile-derived columns when it writes a
    single-source artifact. ``expansion_failure_reason`` /
    ``no_valid_reason`` are ``None`` for the common case where the tool
    expanded and validated cleanly; this is preserved through JSON as
    ``null`` and rendered as empty string in TSV.
    """
    row: dict[str, str | int | None] = {
        "repo": display_name,
        "version": version,
        "path": str(path.relative_to(repo_dir)),
        "tool_id": stats.tool_id,
        "sha256": sha,
        "profile_raw": stats.profile_raw,
        "profile_expanded": stats.profile_expanded,
        "newest_valid": stats.newest_valid,
        "expansion_failure_reason": stats.expansion_failure_reason,
        "no_valid_reason": stats.no_valid_reason,
    }
    for profile, ok in zip(available_profiles(), stats.validity, strict=True):
        row[_validity_column(profile)] = 1 if ok else 0
    return row


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
            profile_expanded, tool_id, expansion_reason = _expanded_attrs(
                path, document, has_macros_flag=has_macros_flag
            )
            no_valid_reason = (
                _no_valid_reason(
                    path, document, expansion_failure_reason=expansion_reason
                )
                if newest_valid == _PROFILE_NONE
                else None
            )
            stats: ToolStats | None = ToolStats(
                profile_raw=document.root.get("profile") or _PROFILE_NONE,
                profile_expanded=profile_expanded,
                tool_id=tool_id,
                newest_valid=newest_valid,
                validity=vector,
                has_macros=has_macros_flag,
                contiguous=contiguous,
                expansion_failure_reason=expansion_reason,
                no_valid_reason=no_valid_reason,
            )
        else:
            stats = None
        for category, detail in (
            check_immutable(document),
            check_roundtrip(document),
            check_model(document),
            check_parse_load_agree(path),
            check_macro_handling(path, document),
            check_newest_valid_profile(path, vector),
        ):
            if category != "ok":
                return category, detail, category, stats
        if not contiguous:
            run = "".join("1" if ok else "0" for ok in vector)
            return "non-contiguous", f"validity vector: {run}", "non-contiguous", stats
    except Exception as exc:  # noqa: BLE001 — diagnostic sweep: every crash is a finding
        return "crash", traceback.format_exc(), _signature(exc), None
    return "ok", "", "", stats


def _process_path(
    path: Path,
    *,
    source_label: str,
    display_name: str,
    repo_dir: Path,
    version: str,
    state: _SweepState,
    combined: bool,
    collect_stats: bool,
    need_sha: bool,
) -> bool:
    """Sweep one XML file and update ``state``; return ``True`` if it counts.

    The per-path body of ``main``'s loop, extracted so the main function
    itself stays shallow. Returns ``True`` when the file was a kept
    ``<tool>`` (so the caller increments its tools / per-repo counters),
    or ``False`` when it was a duplicate of an already-seen sha, a
    non-file path, or a non-tool XML.
    """
    if not path.is_file():
        return False
    sha = hashlib.sha256(path.read_bytes()).hexdigest() if need_sha else ""
    if combined and sha in state.seen_hashes:
        state.source_duplicate_counts[source_label] += 1
        cached = state.sha_to_stats.get(sha)
        if cached is not None:
            state.rows.append(
                _make_row(
                    display_name=display_name,
                    version=version,
                    path=path,
                    repo_dir=repo_dir,
                    sha=sha,
                    stats=cached,
                )
            )
        return False
    if combined:
        state.seen_hashes.add(sha)
    status, detail, signature, stats = _exercise(path, collect_stats=collect_stats)
    if status == "skip":
        return False
    state.source_unique_counts[source_label] += 1
    if stats is not None:
        state.declared_raw_counts[stats.profile_raw] += 1
        state.declared_expanded_counts[stats.profile_expanded] += 1
        state.newest_valid_counts[stats.newest_valid] += 1
        state.crosstab[(stats.profile_expanded, stats.newest_valid)] += 1
        state.macro_counts[stats.has_macros] += 1
        state.contiguity_counts[stats.contiguous] += 1
        if stats.expansion_failure_reason is not None:
            state.expansion_failure_counts[stats.expansion_failure_reason] += 1
        if stats.no_valid_reason is not None:
            state.no_valid_counts[stats.no_valid_reason] += 1
        if combined:
            state.sha_to_stats[sha] = stats
        state.rows.append(
            _make_row(
                display_name=display_name,
                version=version,
                path=path,
                repo_dir=repo_dir,
                sha=sha,
                stats=stats,
            )
        )
    if status == "ok":
        return True
    state.signatures[signature] += 1
    if signature in state.retained_signatures:
        return True
    state.retained_signatures.add(signature)
    # display_name may carry a '/' for toolshed (owner/name); sanitize so
    # retained fixture directories stay flat.
    dest = _retain(path, display_name.replace("/", "__"))
    relative = path.relative_to(repo_dir)
    state.retained.append((dest.name, display_name, relative, version, signature))
    logger.warning(
        "%s [%s] %s\n  %s\n  retained -> %s\n  %s",
        status.upper(),
        display_name,
        signature,
        relative,
        dest,
        detail.strip().replace("\n", "\n  "),
    )
    return True


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


def _failure_slug(reason: str) -> str:
    """Return a filesystem- and URL-friendly slug for a failure category."""
    slug = re.sub(r"[^a-z0-9]+", "-", reason.lower()).strip("-")
    return slug or "unknown"


def _format_reason_table(
    title: str,
    intro: str,
    counts: Counter[str],
    *,
    link_base: str | None = None,
) -> list[str]:
    """Render a failure-reason breakdown as a markdown table.

    Percentages are of the category total (sum of ``counts``), not of
    the whole sweep — these tables answer "how do tools in this failure
    category split up?". A bold total row is appended. When
    ``link_base`` is provided each reason cell becomes a markdown link
    to ``<link_base>/<slug>.md`` so a reader can drill into the failing
    tools for that category.
    """
    lines = [f"## {title}", "", intro, "", "| Reason | Tools | % |", "|---|---:|---:|"]
    total = sum(counts.values())
    if total == 0:
        lines.append("| _(none)_ |  |  |")
        return lines
    for reason in sorted(counts, key=lambda r: (-counts[r], r)):
        n = counts[reason]
        label = (
            f"[{reason}]({link_base}/{_failure_slug(reason)}.md)"
            if link_base is not None
            else reason
        )
        lines.append(f"| {label} | {n} | {n / total * 100:.1f}% |")
    lines.append(f"| **total** | **{total}** | **100.0%** |")
    return lines


def _tool_source_url(repo: str, version: str, path: str) -> str | None:
    """Return a clickable URL to ``path`` in ``repo`` at ``version``, or ``None``.

    ``repo`` containing a ``/`` is treated as a toolshed ``owner/name``;
    everything else is looked up in ``corpus_sources.json`` and mapped
    to the upstream host's web view (github.com or gitlab.com). Returns
    ``None`` when the upstream host is unrecognised, **or** when
    ``version`` is the ``_UNKNOWN`` sentinel that `fetch_toolshed.py`
    writes for clones predating the manifest — both would build a
    deterministically-broken URL, so refusing to render a link is more
    honest than emitting a 404.
    """
    if version == _UNKNOWN:
        return None
    if "/" in repo:
        # `version` and `path` are intentionally not embedded: the toolshed
        # only exposes the `/view/<owner>/<name>` UI publicly. The user
        # navigates to the file from the repo's browse page (the path is
        # rendered separately in the table for that purpose).
        return f"{_TOOLSHED_VIEW_URL}/{repo}"
    sources = dict(_corpus_sources())
    clone_url = sources.get(repo)
    if clone_url is None:
        return None
    base = clone_url.removesuffix(".git").rstrip("/")
    if "github.com/" in base:
        return f"{base}/blob/{version}/{path}"
    if "gitlab.com/" in base:
        return f"{base}/-/blob/{version}/{path}"
    return None


def _write_failure_details(
    rows: list[dict[str, str | int | None]],
    *,
    output_dir: Path,
) -> None:
    """Write per-failure-mode markdown indexes under ``output_dir``.

    For each unique failure-reason category, write a markdown file
    listing every failing tool with a link to the upstream source. A
    ``README.md`` index links every category file with its count. Tools
    are deduplicated by sha256 (the first occurrence wins) so the file
    counts match the aggregate stats markdown.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    by_reason: dict[str, list[dict[str, str | int | None]]] = {}
    for row in rows:
        sha = row.get("sha256")
        if not isinstance(sha, str) or sha in seen:
            continue
        seen.add(sha)
        # Each unique tool may land in two files: its expansion-failure
        # sub-category (Group A) and / or its no-valid-reason category.
        # Group A tools therefore appear in both their granular sub-page
        # AND the "(macro expansion failed)" umbrella page so every link
        # from the combined stats markdown resolves.
        for reason in (
            row.get("expansion_failure_reason"),
            row.get("no_valid_reason"),
        ):
            if isinstance(reason, str):
                by_reason.setdefault(reason, []).append(row)
    index_lines = [
        "# Failure-mode tool indexes",
        "",
        "One file per failure-reason category — click through to see the "
        "failing tools at their upstream source. Tools are deduplicated by "
        "sha256 (one entry per unique bytes); the link points to whichever "
        "source repository was first seen for that tool in the combined "
        "sweep. This index is regenerated by every full "
        "`corpus_check.py --source combined` run.",
        "",
        "| Category | Tools | File |",
        "|---|---:|---|",
    ]
    for reason in sorted(by_reason, key=lambda r: (-len(by_reason[r]), r)):
        slug = _failure_slug(reason)
        index_lines.append(
            f"| {reason} | {len(by_reason[reason])} | [{slug}.md]({slug}.md) |"
        )
    (output_dir / "README.md").write_text(
        "\n".join(index_lines) + "\n", encoding="utf-8"
    )
    for reason, reason_rows in by_reason.items():
        slug = _failure_slug(reason)
        lines = [
            f"# {reason}",
            "",
            f"{len(reason_rows)} unique tool(s) fall into this category. Each "
            "link goes to the source-repository view of the tool at the "
            "version captured by the combined sweep.",
            "",
            "| Repository | tool_id | Path | Version | Source |",
            "|---|---|---|---|---|",
        ]
        for row in sorted(reason_rows, key=lambda r: (str(r["repo"]), str(r["path"]))):
            repo = str(row["repo"])
            version = str(row["version"])
            path = str(row["path"])
            tool_id = str(row["tool_id"])
            url = _tool_source_url(repo, version, path)
            link = f"[view]({url})" if url else "—"
            lines.append(
                f"| {repo} | `{tool_id}` | `{path}` | `{version[:12]}` | {link} |"
            )
        (output_dir / f"{slug}.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )


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


def _tsv_safe(value: str) -> str:
    """Replace tab/newline/CR with a single space — defensive TSV escape.

    Real corpus tools don't contain these characters in any of the columns
    we emit, but TSV has no quoting standard, so any one of them would
    silently break a downstream parser. The sanitisation is expected to
    be a no-op on every row.
    """
    return value.replace("\t", " ").replace("\n", " ").replace("\r", " ")


def _write_corpus_data(
    *,
    rows: list[dict[str, str | int | None]],
    source: str,
    include_profile_columns: bool,
) -> None:
    """Write the fine-grained per-tool data as both JSON and TSV.

    The per-source artifacts carry only the identifying columns
    (``repo, version, path, tool_id, sha256``); the combined artifact
    appends the profile columns (``profile_raw, profile_expanded,
    newest_valid, expansion_failure_reason, no_valid_reason``) plus one
    ``valid_<profile>`` column per vendored profile (``0`` / ``1``,
    oldest profile first). JSON: an array of objects with the schema's
    column order preserved; validity flags are JSON integers, missing
    failure-reasons are ``null``. TSV: header row plus one row per
    tool, UTF-8, LF endings, with tab / newline / CR replaced by a
    single space in field values and ``None`` rendered as the empty
    string.
    """
    columns: tuple[str, ...] = _FINE_GRAINED_BASE_COLUMNS
    if include_profile_columns:
        columns = (
            columns
            + _FINE_GRAINED_PROFILE_COLUMNS
            + tuple(_validity_column(profile) for profile in available_profiles())
        )
    _CORPUS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    basename = _CORPUS_DATA_BASENAMES[source]
    projected = [{column: row[column] for column in columns} for row in rows]
    (_CORPUS_DATA_DIR / f"{basename}.json").write_text(
        json.dumps(projected, indent=2) + "\n", encoding="utf-8"
    )
    lines = ["\t".join(columns)]
    lines.extend(
        "\t".join(
            _tsv_safe("" if record[column] is None else str(record[column]))
            for column in columns
        )
        for record in projected
    )
    (_CORPUS_DATA_DIR / f"{basename}.tsv").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


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
    expansion_failure_counts: Counter[str],
    no_valid_counts: Counter[str],
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
    if source == "combined":
        link_base = f"corpus_data/{_FAILURE_DETAILS_SUBDIR}"
        lines.extend(
            _format_reason_table(
                "Macro-expansion failure reasons",
                "Tools whose macros could not be expanded by "
                "`galaxy.util.xml_macros` — the post-expansion tree never "
                "reaches the XSD. The reason comes from the first "
                "`MacroError` returned by the adapter; these are properties "
                "of the tool itself (or its `<import>`s), not library bugs. "
                "Click a reason to see the failing tools at their upstream "
                "source.",
                expansion_failure_counts,
                link_base=link_base,
            )
        )
        lines.append("")
        lines.extend(
            _format_reason_table(
                "Tools with no valid vendored profile — reason breakdown",
                "Tools whose validity vector is empty (no vendored XSD "
                "accepts them). The reason is derived from the first "
                "schema error reported at the tool's declared profile "
                "(falling back to the latest profile if none is declared); "
                "macro-expansion-failed tools are aggregated under "
                "`(macro expansion failed)` — see the section above for "
                "their breakdown. Click a reason to see the failing tools "
                "at their upstream source.",
                no_valid_counts,
                link_base=link_base,
            )
        )
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
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if args.repo is not None and args.source != "github":
        logger.error(
            "--repo is only supported with --source github, not %r", args.source
        )
        return 1
    if args.source == "github" and args.repo is not None:
        known_names = {name for name, _ in _corpus_sources()}
        if args.repo not in known_names:
            known = ", ".join(sorted(known_names))
            logger.error("unknown --repo %r; known: %s", args.repo, known)
            return 1

    _CORPUS_ROOT.mkdir(parents=True, exist_ok=True)
    stats_file = _STATS_FILES[args.source]

    # The stats artifact is only written for full sweeps, so skip the
    # per-tool stats work (one macro expansion each) in any other case.
    collect_stats = not (args.no_stats or args.limit or args.repo)
    combined = args.source == "combined"
    sources_to_walk = _COMBINED_SUB_SOURCES if combined else (args.source,)
    if "toolshed" in sources_to_walk and not _TOOLSHED_ROOT.exists():
        logger.error(
            "no toolshed corpus at %s; run scripts/fetch_toolshed.py first",
            _TOOLSHED_ROOT.relative_to(_REPO_ROOT),
        )
        return 1
    tools = 0
    repo_tool_counts: list[tuple[str, str, int]] = []
    state = _SweepState(retained_signatures=_known_signatures())
    need_sha = combined or collect_stats
    # args.repo is only reachable here when args.source == "github"
    # (the early CLI guard rejects it for any other source), so combined
    # mode always sees args.repo is None — no need to mask it.
    for source_label, display_name, repo_dir, version in _iter_sources(
        sources_to_walk, repo_filter=args.repo
    ):
        repo_tool_count = 0
        for path in sorted(repo_dir.rglob("*.xml")):
            if args.limit and tools >= args.limit:
                break
            if not _process_path(
                path,
                source_label=source_label,
                display_name=display_name,
                repo_dir=repo_dir,
                version=version,
                state=state,
                combined=combined,
                collect_stats=collect_stats,
                need_sha=need_sha,
            ):
                continue
            tools += 1
            repo_tool_count += 1
            if tools % 500 == 0:
                logger.info("... %d tools", tools)
        repo_tool_counts.append((display_name, version, repo_tool_count))
        if args.limit and tools >= args.limit:
            break

    logger.info("swept %d tools", tools)
    for signature, count in state.signatures.most_common():
        logger.info("  %6d  %s", count, signature)
    if "non-contiguous" in state.signatures:
        logger.info(
            "note: non-contiguous validity is an expected real-world property, "
            "not a library bug — newest_valid_profile handles it."
        )
    if state.retained:
        _append_provenance(state.retained)
        logger.info(
            "retained %d new regression fixture(s) under %s",
            len(state.retained),
            _REGRESSIONS,
        )
    stats_path = stats_file.relative_to(_REPO_ROOT)
    data_basename = _CORPUS_DATA_BASENAMES[args.source]
    data_dir_rel = _CORPUS_DATA_DIR.relative_to(_REPO_ROOT)
    if args.no_stats:
        pass
    elif args.limit or args.repo:
        logger.info(
            "corpus stats not regenerated: partial sweep (--limit or --repo). "
            "Run the full sweep to refresh %s and %s/%s.{json,tsv}.",
            stats_path,
            data_dir_rel,
            data_basename,
        )
    else:
        _write_stats(
            stats_file=stats_file,
            source=args.source,
            repos=repo_tool_counts,
            declared_raw_counts=state.declared_raw_counts,
            declared_expanded_counts=state.declared_expanded_counts,
            newest_valid_counts=state.newest_valid_counts,
            crosstab=state.crosstab,
            macro_counts=state.macro_counts,
            contiguity_counts=state.contiguity_counts,
            include_raw=args.include_raw_profile,
            total=tools,
            source_unique_counts=state.source_unique_counts,
            source_duplicate_counts=state.source_duplicate_counts,
            expansion_failure_counts=state.expansion_failure_counts,
            no_valid_counts=state.no_valid_counts,
        )
        _write_corpus_data(
            rows=state.rows,
            source=args.source,
            include_profile_columns=args.source == "combined",
        )
        logger.info("corpus stats -> %s", stats_path)
        logger.info("corpus data  -> %s/%s.{json,tsv}", data_dir_rel, data_basename)
        if args.source == "combined":
            failures_dir = _CORPUS_DATA_DIR / _FAILURE_DETAILS_SUBDIR
            _write_failure_details(rows=state.rows, output_dir=failures_dir)
            logger.info("failure details -> %s/", failures_dir.relative_to(_REPO_ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
