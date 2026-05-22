"""``ToolDocument`` — the mutable representation of a parsed Galaxy tool.

A ``ToolDocument`` wraps the parsed lxml tree, which is the **source of truth**:
lxml preserves CDATA sections, comments, attribute data, and attribute order
exactly, so a downstream formatter can mutate and re-serialise it faithfully.
There is deliberately no serialization method — exposing the tree *is* the
contract owed to that downstream project.

``model()`` returns a derived, read-only xsdata typed view for the tool's
profile, re-bound from the current tree on demand.
"""

from __future__ import annotations

import dataclasses
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from lxml import etree
from xsdata.formats.dataclass.parsers import XmlParser
from xsdata.formats.dataclass.parsers.config import ParserConfig

from galaxy_tool_xml.models.registry import tool_class
from galaxy_tool_xml.profiles import resolve_profile

if TYPE_CHECKING:
    from galaxy_tool_xml.models.any_tool import AnyTool


def _lenient_class_factory(clazz: type[Any], params: dict[str, Any]) -> Any:
    """Instantiate an xsdata model class, tolerating omitted required fields.

    ``model()`` binds the tree without expanding macros, so an element a macro
    would supply — a ``<conditional>``'s ``<param>``, say — can be absent even
    though the schema marks it required and the generated dataclass gives it no
    default. Default such a field to ``None`` so binding yields a partial view
    rather than raising ``TypeError``.
    """
    for model_field in dataclasses.fields(clazz):
        if (
            model_field.name not in params
            and model_field.default is dataclasses.MISSING
            and model_field.default_factory is dataclasses.MISSING
        ):
            params[model_field.name] = None
    return clazz(**params)


@cache
def _xml_parser() -> XmlParser:
    """Return a shared, lenient xsdata parser (cached after first call).

    The parser is lenient on unknown elements and attributes, and fills omitted
    required fields (see ``_lenient_class_factory``), so binding never fails on a
    tool that diverges from its profile's model — for instance an un-expanded
    tool still carrying macro elements.
    """
    config = ParserConfig(
        fail_on_unknown_properties=False,
        fail_on_unknown_attributes=False,
        class_factory=_lenient_class_factory,
    )
    return XmlParser(config=config)


class ToolDocument:
    """A parsed Galaxy tool document wrapping a mutable lxml tree."""

    def __init__(
        self, tree: etree._ElementTree, *, source_path: Path | None = None
    ) -> None:
        self._tree = tree
        self._source_path = source_path

    @property
    def tree(self) -> etree._ElementTree:
        """The mutable lxml ``ElementTree`` — the source of truth."""
        return self._tree

    @property
    def root(self) -> etree._Element:
        """The root ``<tool>`` element of the mutable tree."""
        return self._tree.getroot()

    @property
    def source_path(self) -> Path | None:
        """The file the document was loaded from, or ``None`` for in-memory input.

        ``validate_tool`` uses this to resolve macro ``<import>``s relative to
        the tool's own directory, since a mutated tree no longer matches its
        original file on disk.
        """
        return self._source_path

    @property
    def profile(self) -> str | None:
        """The tool's ``profile`` attribute, or ``None`` if it is absent."""
        profile: str | None = self.root.get("profile")
        return profile

    def model(self, *, version: str | None = None) -> AnyTool:
        """Bind the current tree to the profile's xsdata ``Tool`` model.

        Binds against the typed model for the tool's resolved ``profile``, or for
        an explicit ``version`` override (a vendored version string). The result
        is a read-only view, re-derived on every call from the live tree;
        callers may cache it. The lxml tree, not this model, is the mutable
        representation.
        """
        resolved = resolve_profile(version if version is not None else self.profile)
        # The exact runtime class is version-specific; cast to the AnyTool union.
        return cast(
            "AnyTool",
            _xml_parser().from_bytes(etree.tostring(self.root), tool_class(resolved)),
        )
