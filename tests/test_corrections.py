"""Tests for near-miss typo suggestions."""

from pathlib import Path

from galaxy_tool_xml.corrections import suggest_corrections


def test_typos_detected_for_all_kinds(data_dir: Path) -> None:
    corrections = suggest_corrections(data_dir / "tool_with_typos.xml")
    kinds = {correction.kind for correction in corrections}
    assert kinds == {"attribute", "element", "enum_value"}


def test_typo_suggestions_point_at_correct_spelling(data_dir: Path) -> None:
    corrections = suggest_corrections(data_dir / "tool_with_typos.xml")
    suggested = {correction.suggested for correction in corrections}
    assert {"optional", "description", "data_source"} <= suggested


def test_clean_tool_yields_no_corrections(data_dir: Path) -> None:
    assert suggest_corrections(data_dir / "minimal_tool.xml") == []


def test_genuinely_unknown_name_yields_no_correction() -> None:
    xml = (
        b'<tool id="t" name="T" version="1.0.0" profile="26.0">'
        b"<zzzzzzzzzz/></tool>"
    )
    corrections = suggest_corrections(xml)
    assert all(correction.kind != "element" for correction in corrections)


def test_macro_constructs_not_flagged(data_dir: Path) -> None:
    corrections = suggest_corrections(data_dir / "tool_with_macros.xml")
    assert corrections == []


def test_correction_str_is_a_did_you_mean_line(data_dir: Path) -> None:
    corrections = suggest_corrections(data_dir / "tool_with_typos.xml")
    assert all("did you mean" in str(correction) for correction in corrections)
