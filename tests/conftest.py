"""Shared pytest fixtures and configuration."""

import sys
from pathlib import Path

import pytest

# Put the repo root on sys.path so the suite can import the maintainer scripts:
# tests/test_regressions.py reuses the invariant checks from scripts/corpus_check.py.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture
def data_dir() -> Path:
    """The directory holding the tool-XML test fixtures."""
    return Path(__file__).parent / "data"
