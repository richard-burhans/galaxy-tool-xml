"""Slow codegen sweep: xsdata must regenerate models from every vendored XSD.

Deselected by default (``-m 'not slow'``); run with ``uv run pytest -m slow``.

xsdata caches its resolved output path across calls in one process, so each
schema is generated in a fresh subprocess rather than via ``monkeypatch.chdir``.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCHEMA_DIR = Path(__file__).parent.parent / "src" / "galaxy_tool_xml" / "schema"
_VERSIONS = sorted(
    json.loads((_SCHEMA_DIR / "manifest.json").read_text(encoding="utf-8"))["schemas"]
)

_WORKER = """
import logging, os, py_compile, shutil, sys, tempfile
from pathlib import Path

from xsdata.codegen.transformer import ResourceTransformer
from xsdata.models.config import GeneratorConfig

logging.disable(logging.CRITICAL)
with tempfile.TemporaryDirectory() as tmp:
    staged = Path(tmp) / "galaxy.xsd"
    shutil.copy(sys.argv[1], staged)
    os.chdir(tmp)
    config = GeneratorConfig()
    config.output.package = "generated"
    config.output.unnest_classes = True
    ResourceTransformer(config=config).process([staged.as_uri()])
    for module in (Path(tmp) / "generated").rglob("*.py"):
        py_compile.compile(str(module), doraise=True)
"""


@pytest.mark.slow
@pytest.mark.parametrize("version", _VERSIONS)
def test_codegen_succeeds_for_every_vendored_xsd(version: str) -> None:
    schema = _SCHEMA_DIR / f"galaxy-{version}.xsd"
    result = subprocess.run(
        [sys.executable, "-c", _WORKER, str(schema)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
