"""Unit tests for the corpus-stats fine-grained emitter helpers.

End-to-end behaviour is covered by a full ``scripts/corpus_check.py``
sweep, but the per-source vs combined column shape, the per-profile
validity flags, and the TSV escaping are each small enough to be worth
covering in isolation so a schema regression surfaces without paying
for a multi-minute sweep.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import scripts.corpus_check as corpus_check
from scripts.corpus_check import (
    ToolStats,
    _make_row,
    _tsv_safe,
    _write_corpus_data,
)

from galaxy_tool_xml.profiles import available_profiles


def _stats(*, validity: list[bool] | None = None) -> ToolStats:
    """Build a ToolStats with a default-everything-False validity vector."""
    profiles = available_profiles()
    return ToolStats(
        profile_raw="@PROFILE@",
        profile_expanded="25.0",
        tool_id="kegalign",
        newest_valid="26.1",
        validity=validity if validity is not None else [False] * len(profiles),
        has_macros=True,
        contiguous=False,
    )


def _row(**overrides: object) -> dict[str, str | int]:
    """Build one fully-populated combined-schema row with overridable fields."""
    defaults: dict[str, object] = {
        "display_name": "richard-burhans/kegalign",
        "version": "f885abcfe3a0",
        "path": Path("/repo/tool.xml"),
        "repo_dir": Path("/repo"),
        "sha": "abc",
        "stats": _stats(),
    }
    defaults.update(overrides)
    return _make_row(**defaults)  # type: ignore[arg-type]


def test_make_row_carries_the_full_combined_schema() -> None:
    profiles = available_profiles()
    row = _row()
    assert list(row.keys())[:8] == [
        "repo",
        "version",
        "path",
        "tool_id",
        "sha256",
        "profile_raw",
        "profile_expanded",
        "newest_valid",
    ]
    assert len(row) == 8 + len(profiles)
    for profile in profiles:
        assert f"valid_{profile}" in row


def test_make_row_path_is_relative_to_repo_dir() -> None:
    row = _row(path=Path("/repo/tools/sub/tool.xml"), repo_dir=Path("/repo"))
    assert row["path"] == "tools/sub/tool.xml"


def test_make_row_propagates_identifying_fields() -> None:
    row = _row(display_name="some/repo", version="abcdef012345", sha="dead")
    assert row["repo"] == "some/repo"
    assert row["version"] == "abcdef012345"
    assert row["sha256"] == "dead"
    assert row["tool_id"] == "kegalign"


def test_make_row_validity_flags_are_int_zero_or_one() -> None:
    profiles = available_profiles()
    pattern = [bool(i % 2) for i in range(len(profiles))]
    row = _row(stats=_stats(validity=pattern))
    for profile, ok in zip(profiles, pattern, strict=True):
        value = row[f"valid_{profile}"]
        assert isinstance(value, int)
        assert value == (1 if ok else 0)


def test_tsv_safe_replaces_tab_newline_cr_with_space() -> None:
    assert _tsv_safe("a\tb") == "a b"
    assert _tsv_safe("a\nb") == "a b"
    assert _tsv_safe("a\rb") == "a b"
    assert _tsv_safe("a\t\n\rb") == "a   b"
    assert _tsv_safe("ordinary value") == "ordinary value"


def _two_rows() -> list[dict[str, str | int]]:
    profiles = available_profiles()
    return [
        _row(sha="sha_a", stats=_stats(validity=[True] * len(profiles))),
        _row(
            display_name="other/repo",
            version="aaaaaaaaaaaa",
            path=Path("/repo/b.xml"),
            sha="sha_b",
            stats=_stats(validity=[False] * len(profiles)),
        ),
    ]


@pytest.fixture
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``_CORPUS_DATA_DIR`` at the module level for one test."""
    monkeypatch.setattr(corpus_check, "_CORPUS_DATA_DIR", tmp_path)
    return tmp_path


def test_write_corpus_data_per_source_omits_profile_columns(
    isolated_data_dir: Path,
) -> None:
    _write_corpus_data(rows=_two_rows(), source="github", include_profile_columns=False)
    data = json.loads(
        (isolated_data_dir / "corpus_data.json").read_text(encoding="utf-8")
    )
    assert len(data) == 2
    assert list(data[0].keys()) == ["repo", "version", "path", "tool_id", "sha256"]


def test_write_corpus_data_combined_includes_validity_flags(
    isolated_data_dir: Path,
) -> None:
    _write_corpus_data(
        rows=_two_rows(), source="combined", include_profile_columns=True
    )
    data = json.loads(
        (isolated_data_dir / "combined_corpus_data.json").read_text(encoding="utf-8")
    )
    profiles = available_profiles()
    assert len(data[0]) == 8 + len(profiles)
    # Row 0 was built with validity=[True, ...]; row 1 with [False, ...].
    for profile in profiles:
        assert data[0][f"valid_{profile}"] == 1
        assert data[1][f"valid_{profile}"] == 0


def test_write_corpus_data_combined_tsv_has_header_plus_one_row_per_record(
    isolated_data_dir: Path,
) -> None:
    _write_corpus_data(
        rows=_two_rows(), source="combined", include_profile_columns=True
    )
    lines = (
        (isolated_data_dir / "combined_corpus_data.tsv")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    profiles = available_profiles()
    expected_columns = 8 + len(profiles)
    assert len(lines) == 3  # header + 2 data rows
    assert lines[0].split("\t")[0] == "repo"
    assert len(lines[0].split("\t")) == expected_columns
    for value in lines[1].split("\t")[8:]:
        assert value == "1"
    for value in lines[2].split("\t")[8:]:
        assert value == "0"


def test_write_corpus_data_tsv_sanitizes_field_values_containing_tabs(
    isolated_data_dir: Path,
) -> None:
    profiles = available_profiles()
    stats = ToolStats(
        profile_raw="(none)",
        profile_expanded="(none)",
        tool_id="bad\ttool\nid",
        newest_valid="(none)",
        validity=[False] * len(profiles),
        has_macros=False,
        contiguous=True,
    )
    row = _row(stats=stats)
    _write_corpus_data(rows=[row], source="github", include_profile_columns=False)
    lines = (
        (isolated_data_dir / "corpus_data.tsv").read_text(encoding="utf-8").splitlines()
    )
    assert len(lines[1].split("\t")) == 5  # tabs in tool_id did not split the row
    tool_id_value = lines[1].split("\t")[3]
    assert "\t" not in tool_id_value
    assert "\n" not in tool_id_value
    assert tool_id_value == "bad tool id"


def test_write_corpus_data_json_uses_native_integers_for_validity_flags(
    isolated_data_dir: Path,
) -> None:
    _write_corpus_data(
        rows=_two_rows(), source="combined", include_profile_columns=True
    )
    raw = (isolated_data_dir / "combined_corpus_data.json").read_text(encoding="utf-8")
    # JSON ints (no surrounding quotes) — verify by string search and by
    # round-tripping back through json.loads.
    for profile in available_profiles():
        assert f'"valid_{profile}": 1' in raw or f'"valid_{profile}": 0' in raw
    data = json.loads(raw)
    sample = next(value for key, value in data[0].items() if key.startswith("valid_"))
    assert isinstance(sample, int)
