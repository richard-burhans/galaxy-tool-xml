"""Build-time codegen: generate the per-version xsdata model packages.

Each vendored XSD becomes its own model package under ``galaxy_tool_xml/models/``
(``v16_10`` … ``v26_0``). xsdata caches its resolved output path within a
process, so every version is generated in a fresh subprocess via this module's
``python -m galaxy_tool_xml._codegen`` entry point.

This module is imported by the hatchling build hook, ``scripts/regenerate.py``,
and the codegen test. It must not import hatchling, and imports xsdata only on
the ``__main__`` path, so merely importing the module stays cheap.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from galaxy_tool_xml.models.registry import version_to_module

_PACKAGE_DIR = Path(__file__).resolve().parent  # src/galaxy_tool_xml
_SRC_DIR = _PACKAGE_DIR.parent  # src
_SCHEMA_DIR = _PACKAGE_DIR / "schema"
_MODELS_DIR = _PACKAGE_DIR / "models"
_MODULE_NAME = "galaxy_tool_xml._codegen"


def _vendored_versions() -> list[str]:
    """Return every vendored version from the schema manifest, oldest first."""
    manifest = json.loads((_SCHEMA_DIR / "manifest.json").read_text(encoding="utf-8"))
    schemas: dict[str, object] = manifest["schemas"]
    return sorted(schemas)


def _run_xsdata(version: str, *, models_dir: Path) -> None:
    """Generate one version's model package as ``models_dir/v{slug}/``.

    Runs xsdata in the current process — only ever called inside the dedicated
    subprocess spawned by ``_generate_one``, because xsdata caches its resolved
    output path process-wide. xsdata writes into a throwaway directory; only the
    leaf package is copied into ``models_dir``, so hand-written files alongside
    it (``__init__.py``, ``registry.py``) are never touched.
    """
    from xsdata.codegen.transformer import ResourceTransformer
    from xsdata.models.config import GeneratorConfig

    module = version_to_module(version)
    config = GeneratorConfig()
    config.output.package = f"galaxy_tool_xml.models.{module}"
    # unnest_classes works around an xsdata 26.2 bug: with nested inner classes
    # its circular-reference detector raises KeyError on the Galaxy 24.2+ schema.
    config.output.unnest_classes = True
    with tempfile.TemporaryDirectory() as tmp:
        staged_xsd = Path(tmp) / "galaxy.xsd"
        shutil.copy(_SCHEMA_DIR / f"galaxy-{version}.xsd", staged_xsd)
        os.chdir(tmp)  # xsdata writes the package tree relative to cwd
        ResourceTransformer(config=config).process([staged_xsd.as_uri()])
        generated = Path(tmp) / "galaxy_tool_xml" / "models" / module
        target = models_dir / module
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(generated, target)


def _generate_one(version: str, *, models_dir: Path) -> None:
    """Generate one version's model package in a fresh subprocess."""
    pythonpath = os.pathsep.join(
        part for part in (str(_SRC_DIR), os.environ.get("PYTHONPATH", "")) if part
    )
    result = subprocess.run(
        [sys.executable, "-m", _MODULE_NAME, version, str(models_dir)],
        env={**os.environ, "PYTHONPATH": pythonpath},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"model codegen failed for Galaxy {version}:\n{result.stderr}"
        )


def _all_present(versions: list[str]) -> bool:
    """Whether every per-version package and ``any_tool.py`` already exist."""
    if not (_MODELS_DIR / "any_tool.py").is_file():
        return False
    return all(
        (_MODELS_DIR / version_to_module(version)).is_dir() for version in versions
    )


def _write_any_tool(versions: list[str]) -> None:
    """Write ``models/any_tool.py`` — the ``AnyTool`` union over every model."""
    lines = ['"""Generated: the union of every per-version ``Tool`` model."""', ""]
    aliases: list[str] = []
    for version in versions:
        module = version_to_module(version)
        alias = f"_{module}"
        lines.append(f"from galaxy_tool_xml.models.{module} import Tool as {alias}")
        aliases.append(alias)
    lines += ["", f"AnyTool = {' | '.join(aliases)}", ""]
    (_MODELS_DIR / "any_tool.py").write_text("\n".join(lines), encoding="utf-8")


def regenerate_all_models(*, force: bool = False) -> None:
    """Generate every vendored version's model package under ``src/``.

    Skipped when every package and ``any_tool.py`` already exist, unless
    ``force``. Each version is generated in its own subprocess, in parallel.
    """
    versions = _vendored_versions()
    if not force and _all_present(versions):
        return
    workers = min(os.cpu_count() or 4, 8)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        tuple(pool.map(partial(_generate_one, models_dir=_MODELS_DIR), versions))
    _write_any_tool(versions)


def clean_generated() -> None:
    """Remove every generated per-version package and ``any_tool.py``."""
    for version in _vendored_versions():
        shutil.rmtree(_MODELS_DIR / version_to_module(version), ignore_errors=True)
    (_MODELS_DIR / "any_tool.py").unlink(missing_ok=True)


def _main(argv: list[str]) -> int:
    """Entry point: ``python -m galaxy_tool_xml._codegen <version> <models_dir>``."""
    if len(argv) != 3:
        print(
            f"usage: python -m {_MODULE_NAME} <version> <models_dir>",
            file=sys.stderr,
        )
        return 2
    logging.disable(logging.CRITICAL)  # xsdata is verbose; stay quiet on success
    _run_xsdata(argv[1], models_dir=Path(argv[2]).resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
