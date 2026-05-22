"""``ToolDocument`` — the mutable representation of a parsed Galaxy tool.

A ``ToolDocument`` wraps the parsed lxml tree, which is the **source of truth**:
lxml preserves CDATA sections, comments, attribute data, and attribute order
exactly, so a downstream formatter can mutate and re-serialise it faithfully.
There is deliberately no serialization method — exposing the tree *is* the
contract owed to that downstream project.

``model()`` returns a derived, read-only xsdata typed view, re-bound from the
current tree on demand.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from lxml import etree
from xsdata.formats.dataclass.parsers import XmlParser
from xsdata.formats.dataclass.parsers.config import ParserConfig

from galaxy_tool_xml.models import Tool


@cache
def _xml_parser() -> XmlParser:
    """Return a shared, lenient xsdata parser (cached after first call).

    The parser is lenient on unknown elements and attributes so a tool of any
    profile binds against the single latest-XSD model.
    """
    config = ParserConfig(
        fail_on_unknown_properties=False,
        fail_on_unknown_attributes=False,
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

    def model(self) -> Tool:
        """Bind the current tree to the xsdata ``Tool`` model — a read-only view.

        Re-derived on every call from the live tree; callers may cache the
        result. The lxml tree, not this model, is the mutable representation.
        """
        return _xml_parser().from_bytes(etree.tostring(self.root), Tool)
