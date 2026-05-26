#!/usr/bin/env python3
"""Fetch latest revisions of every Galaxy ToolShed repository into ``corpus/``.

A standalone maintenance script — Python standard library for the API
listing, plus the ``hg`` binary shipped by the ``mercurial`` dev dependency
for cloning. ``corpus/`` is gitignored; this script populates it for deep
testing and stats work, parallel to ``fetch_schemas.py``.

The Galaxy ToolShed exposes content only over the Mercurial wire protocol
(its public API has no tarball or raw-file endpoint), so each repository is
fetched via ``hg clone``. ``.hg/`` is removed after each clone so that each
``corpus/galaxy-toolshed/<owner>/<name>/`` directory holds only the latest
revision's files — matching the "latest version per tool" rule used by the
corpus stats path.

Default behaviour is additive: existing per-repo directories are reused.
Use ``--force`` to remove and re-clone everything. ``--limit N`` caps the
sweep for sampling; ``--skip-owners`` excludes specific owners.

Usage::

    uv run python scripts/fetch_toolshed.py [--limit N] [--force] \
        [--skip-owners OWNER,OWNER,...]
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger("fetch_toolshed")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CORPUS_ROOT = _REPO_ROOT / "corpus"
_TOOLSHED_ROOT = _CORPUS_ROOT / "galaxy-toolshed"
_TOOLSHED_HOST = "https://toolshed.g2.bx.psu.edu"
_API_ENDPOINT = f"{_TOOLSHED_HOST}/api/repositories"
_USER_AGENT = "galaxy-tool-xml-fetch-toolshed"
_HTTP_TIMEOUT = 30
_PAGE_SIZE = 500
_PROGRESS_EVERY = 100


def _api_get(url: str) -> dict[str, object]:
    """GET a ToolShed API URL, returning the parsed JSON envelope."""
    request = urllib.request.Request(url)  # noqa: S310 — fixed toolshed host
    request.add_header("User-Agent", _USER_AGENT)
    request.add_header("Accept", "application/json")
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:  # noqa: S310
        body = response.read()
    return json.loads(body)


def list_repositories(*, skip_owners: frozenset[str]) -> list[tuple[str, str]]:
    """Return ``(owner, name)`` for every non-deleted, non-deprecated repo.

    Pagination follows the ToolShed envelope's ``total_results`` until the
    last page is reached. Repos whose owner is in ``skip_owners`` are
    excluded inline so the returned list is the actual fetch target list.
    """
    repos: list[tuple[str, str]] = []
    page = 1
    while True:
        envelope = _api_get(
            f"{_API_ENDPOINT}"
            f"?deleted=false&deprecated=false"
            f"&page={page}&page_size={_PAGE_SIZE}"
        )
        hits = envelope.get("hits") or []
        if not isinstance(hits, list) or not hits:
            break
        for repo in hits:
            if not isinstance(repo, dict):
                continue
            owner = repo.get("owner")
            name = repo.get("name")
            if not owner or not name or owner in skip_owners:
                continue
            repos.append((owner, name))
        total = envelope.get("total_results", 0)
        if not isinstance(total, int) or page * _PAGE_SIZE >= total:
            break
        page += 1
    return repos


def _hg_clone(url: str, dest: Path) -> bool:
    """Clone a Mercurial repo to ``dest`` quietly; return success."""
    result = subprocess.run(
        ["hg", "clone", "--quiet", url, str(dest)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.warning(
            "clone failed (%s): %s",
            url,
            result.stderr.strip() or "<no stderr>",
        )
        return False
    return True


def _fetch_one(owner: str, name: str, *, force: bool) -> str:
    """Fetch one repo into the toolshed corpus tree.

    Returns ``"ok"`` on a fresh successful clone, ``"skipped"`` when the
    destination already exists (and ``force`` is false), or ``"failed"`` on
    a clone error. A failed clone leaves no partial directory behind.
    """
    dest = _TOOLSHED_ROOT / owner / name
    if dest.exists():
        if not force:
            return "skipped"
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"{_TOOLSHED_HOST}/repos/{owner}/{name}"
    if not _hg_clone(url, dest):
        if dest.exists():
            shutil.rmtree(dest)
        return "failed"
    # Drop Mercurial history; we keep only the latest snapshot per
    # corpus-stats convention (and to save ~80% on disk).
    hg_dir = dest / ".hg"
    if hg_dir.exists():
        shutil.rmtree(hg_dir)
    return "ok"


def main(argv: list[str]) -> int:
    """Fetch repositories; return a process exit code."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(
        description="Fetch latest revisions of every Galaxy ToolShed repository."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="stop after N fresh clones (0 fetches every listed repository)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-clone every repository even if it's already on disk",
    )
    parser.add_argument(
        "--skip-owners",
        default="",
        help="comma-separated list of owner names to exclude (e.g. iuc,devteam)",
    )
    args = parser.parse_args(argv)

    skip_owners = frozenset(
        owner.strip() for owner in args.skip_owners.split(",") if owner.strip()
    )
    if skip_owners:
        logger.info("skipping owners: %s", ", ".join(sorted(skip_owners)))

    try:
        repos = list_repositories(skip_owners=skip_owners)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        logger.error("could not list toolshed repositories: %s", error)
        return 1

    logger.info("toolshed lists %d eligible repositories", len(repos))
    _TOOLSHED_ROOT.mkdir(parents=True, exist_ok=True)

    counts = {"ok": 0, "skipped": 0, "failed": 0}
    for owner, name in repos:
        outcome = _fetch_one(owner, name, force=args.force)
        counts[outcome] += 1
        processed = counts["ok"] + counts["failed"]
        if processed and processed % _PROGRESS_EVERY == 0:
            logger.info(
                "  ... %d cloned, %d skipped, %d failed",
                counts["ok"],
                counts["skipped"],
                counts["failed"],
            )
        if args.limit and counts["ok"] >= args.limit:
            logger.info("--limit %d reached, stopping", args.limit)
            break

    logger.info(
        "fetch complete: %d cloned, %d skipped, %d failed (of %d processed)",
        counts["ok"],
        counts["skipped"],
        counts["failed"],
        sum(counts.values()),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
