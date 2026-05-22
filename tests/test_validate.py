"""Tests for profile-aware validation."""

from pathlib import Path

import pytest

from galaxy_tool_xml.binding import load_tool, validate_tool
from galaxy_tool_xml.profiles import UnknownProfileError, latest_profile


def test_declared_profile_selects_matching_schema(data_dir: Path) -> None:
    result = validate_tool(data_dir / "minimal_tool.xml")
    assert result.valid
    assert result.validated
    assert result.schema_version == "24.0"
    assert result.declared_profile == "24.0"


def test_no_profile_tool_uses_latest(data_dir: Path) -> None:
    result = validate_tool(data_dir / "tool_no_profile.xml")
    assert result.schema_version == latest_profile()
    assert result.declared_profile is None
    assert result.valid


def test_old_profile_resolves_to_nearest(data_dir: Path) -> None:
    result = validate_tool(data_dir / "tool_old_profile.xml")
    assert result.declared_profile == "16.04"
    assert result.schema_version == "16.10"
    assert result.valid


def test_profile_argument_overrides(data_dir: Path) -> None:
    result = validate_tool(data_dir / "minimal_tool.xml", profile="26.0")
    assert result.schema_version == "26.0"


def test_on_missing_exact_raises(data_dir: Path) -> None:
    with pytest.raises(UnknownProfileError):
        validate_tool(
            data_dir / "minimal_tool.xml", profile="99.9", on_missing="exact"
        )


def test_invalid_tool_reports_schema_errors(data_dir: Path) -> None:
    result = validate_tool(data_dir / "invalid_tool.xml")
    assert not result.valid
    assert result.validated
    assert result.errors


def test_validate_accepts_mutated_document(data_dir: Path) -> None:
    document = load_tool(data_dir / "minimal_tool.xml")
    document.root.set("name", "Renamed Tool")
    result = validate_tool(document)
    assert result.valid


def test_validate_rejects_bad_macro_handling(data_dir: Path) -> None:
    with pytest.raises(ValueError, match="macro_handling"):
        validate_tool(data_dir / "minimal_tool.xml", macro_handling="bogus")


def test_macro_handling_expand_is_valid(data_dir: Path) -> None:
    result = validate_tool(data_dir / "tool_with_macros.xml")
    assert result.macro_handling == "expand"
    assert result.macros_present
    assert result.validated
    assert result.valid


def test_macro_handling_off_reports_expand_errors(data_dir: Path) -> None:
    result = validate_tool(data_dir / "tool_with_macros.xml", macro_handling="off")
    assert result.validated
    assert not result.valid
    assert result.errors


def test_macro_handling_skip_does_not_validate(data_dir: Path) -> None:
    result = validate_tool(data_dir / "tool_with_macros.xml", macro_handling="skip")
    assert result.macros_present
    assert not result.validated


def test_macro_handling_strip_validates(data_dir: Path) -> None:
    result = validate_tool(data_dir / "tool_with_macros.xml", macro_handling="strip")
    assert result.validated


def test_macro_free_tool_has_macros_present_false(data_dir: Path) -> None:
    result = validate_tool(data_dir / "minimal_tool.xml")
    assert not result.macros_present
