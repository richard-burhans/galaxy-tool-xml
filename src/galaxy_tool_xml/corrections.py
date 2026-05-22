"""Near-miss typo suggestions against the schema vocabulary.

This module is independent of ``validate_tool``: it flags likely misspellings of
attribute names, child-element names, and enumerated attribute values, and
suggests the intended spelling. It only ever *suggests* — it never mutates a
tool.

The schema vocabulary is read by introspecting the xsdata-generated ``models/``
rather than re-parsing the XSD. Because the same tag (``param``, ``data``, …)
means different things under different parents, the tree and the model classes
are descended in lockstep from the ``Tool`` root.
"""

from __future__ import annotations

import dataclasses
import enum
import types
import typing
from dataclasses import dataclass
from difflib import get_close_matches
from functools import cache

from galaxy_tool_xml.binding import Source, parse_tool
from galaxy_tool_xml.document import ToolDocument
from galaxy_tool_xml.models import Tool

# Macro constructs are not part of the post-expansion schema vocabulary; an
# un-expanded tool legitimately contains them, so they are never flagged.
_MACRO_ELEMENTS = frozenset({"expand", "macros", "import", "token", "macro", "xml"})
_CUTOFF = 0.8


@dataclass
class Correction:
    """A single near-miss typo suggestion."""

    line: int
    element: str
    kind: str
    found: str
    suggested: str
    attribute: str | None = None

    def __str__(self) -> str:
        location = f"line {self.line}, <{self.element}>"
        if self.kind == "attribute":
            return (
                f"{location}: unknown attribute '{self.found}' — "
                f"did you mean '{self.suggested}'?"
            )
        if self.kind == "element":
            return (
                f"{location}: unknown child element '{self.found}' — "
                f"did you mean '{self.suggested}'?"
            )
        return (
            f"{location}: unknown value '{self.found}' for attribute "
            f"'{self.attribute}' — did you mean '{self.suggested}'?"
        )


@dataclass(frozen=True)
class _Vocabulary:
    """The attribute and child-element vocabulary of one model class."""

    attributes: dict[str, type[enum.Enum] | None]
    elements: dict[str, type | None]
    has_wildcard: bool


def _unwrap(hint: object) -> object:
    """Strip ``X | None`` and ``list[X]`` wrappers down to a single type."""
    origin = typing.get_origin(hint)
    if origin is types.UnionType or origin is typing.Union:
        args = [arg for arg in typing.get_args(hint) if arg is not type(None)]
        return _unwrap(args[0]) if len(args) == 1 else None
    if origin is list:
        list_args = typing.get_args(hint)
        return _unwrap(list_args[0]) if list_args else None
    return hint


def _enum_type(resolved: object) -> type[enum.Enum] | None:
    """Return ``resolved`` if it is an ``enum.Enum`` subclass, else ``None``."""
    if isinstance(resolved, type) and issubclass(resolved, enum.Enum):
        return resolved
    return None


def _dataclass_or_none(resolved: object) -> type | None:
    """Return ``resolved`` if it is a dataclass type, else ``None``."""
    if isinstance(resolved, type) and dataclasses.is_dataclass(resolved):
        return resolved
    return None


@cache
def _vocabulary(model_class: type) -> _Vocabulary:
    """Introspect a model class into its schema vocabulary (cached per class)."""
    hints = typing.get_type_hints(model_class)
    attributes: dict[str, type[enum.Enum] | None] = {}
    elements: dict[str, type | None] = {}
    has_wildcard = False
    for model_field in dataclasses.fields(model_class):
        kind = model_field.metadata.get("type")
        xml_name = model_field.metadata.get("name") or model_field.name
        resolved = _unwrap(hints.get(model_field.name))
        if kind == "Attribute":
            attributes[xml_name] = _enum_type(resolved)
        elif kind == "Element":
            elements[xml_name] = _dataclass_or_none(resolved)
        elif kind == "Wildcard":
            has_wildcard = True
    return _Vocabulary(
        attributes=attributes, elements=elements, has_wildcard=has_wildcard
    )


def _check_attributes(
    element: typing.Any, model_class: type, corrections: list[Correction]
) -> None:
    """Flag misspelled attribute names and enumerated attribute values."""
    vocabulary = _vocabulary(model_class)
    valid_names = list(vocabulary.attributes)
    for name, value in element.attrib.items():
        if name not in vocabulary.attributes:
            match = get_close_matches(name, valid_names, n=1, cutoff=_CUTOFF)
            if match:
                corrections.append(
                    Correction(
                        line=element.sourceline or 0,
                        element=element.tag,
                        kind="attribute",
                        found=name,
                        suggested=match[0],
                    )
                )
            continue
        enum_class = vocabulary.attributes[name]
        if enum_class is None:
            continue
        legal = [str(member.value) for member in enum_class]
        if value not in legal:
            match = get_close_matches(value, legal, n=1, cutoff=_CUTOFF)
            if match:
                corrections.append(
                    Correction(
                        line=element.sourceline or 0,
                        element=element.tag,
                        kind="enum_value",
                        found=value,
                        suggested=match[0],
                        attribute=name,
                    )
                )


def _walk(
    element: typing.Any, model_class: type, corrections: list[Correction]
) -> None:
    """Descend the tree and the model classes together, collecting corrections."""
    _check_attributes(element, model_class, corrections)
    vocabulary = _vocabulary(model_class)
    for child in element:
        if not isinstance(child.tag, str):
            continue  # comment or processing instruction
        if child.tag in _MACRO_ELEMENTS:
            continue
        if child.tag in vocabulary.elements:
            child_class = vocabulary.elements[child.tag]
            if child_class is not None:
                _walk(child, child_class, corrections)
        elif not vocabulary.has_wildcard:
            match = get_close_matches(
                child.tag, list(vocabulary.elements), n=1, cutoff=_CUTOFF
            )
            if match:
                corrections.append(
                    Correction(
                        line=child.sourceline or 0,
                        element=element.tag,
                        kind="element",
                        found=child.tag,
                        suggested=match[0],
                    )
                )


def suggest_corrections(target: Source | ToolDocument) -> list[Correction]:
    """Return near-miss typo suggestions for a tool.

    ``target`` is a source (path, ``bytes``, or binary stream) or an already
    parsed ``ToolDocument``. The un-expanded tree is walked; macro constructs
    are never flagged. Returns an empty list when the source cannot be parsed.
    """
    document = (
        target if isinstance(target, ToolDocument) else parse_tool(target).document
    )
    if document is None:
        return []
    corrections: list[Correction] = []
    _walk(document.root, Tool, corrections)
    return corrections
