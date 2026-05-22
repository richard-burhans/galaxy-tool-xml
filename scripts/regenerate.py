#!/usr/bin/env python3
"""Regenerate the xsdata typed models from the latest vendored Galaxy XSD.

A maintainer task: run on the ``uv.lock``-pinned toolchain via
``uv run python scripts/regenerate.py``, then commit the regenerated
``src/galaxy_tool_xml/models/`` directory.

The latest vendored XSD is copied into a temp directory under the stable name
``galaxy.xsd`` so the generated module is always ``models/galaxy.py``, even when
the ``latest`` version bumps.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from xsdata.codegen.transformer import ResourceTransformer
from xsdata.models.config import GeneratorConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO_ROOT / "src" / "galaxy_tool_xml" / "schema"


def main() -> int:
    """Generate ``galaxy_tool_xml.models`` from the latest vendored XSD."""
    latest = json.loads((SCHEMA_DIR / "manifest.json").read_text(encoding="utf-8"))[
        "latest"
    ]
    config = GeneratorConfig()
    config.output.package = "galaxy_tool_xml.models"
    # unnest_classes works around an xsdata 26.2 bug: with nested inner classes,
    # its circular-reference detector raises KeyError on the Galaxy 24.2+ schema.
    config.output.unnest_classes = True
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "galaxy.xsd"  # stable name -> stable models/galaxy.py
        shutil.copy(SCHEMA_DIR / f"galaxy-{latest}.xsd", staged)
        os.chdir(REPO_ROOT / "src")  # xsdata writes the package relative to cwd
        ResourceTransformer(config=config).process([staged.as_uri()])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
