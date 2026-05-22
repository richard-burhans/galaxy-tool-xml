# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## Project

`galaxy-tool-xml` is a foundation library and CLI for parsing, profile-aware
validation, and typed inspection of Galaxy tool definition XML. It is the
foundation for a separate, `black`-like Galaxy tool linter/formatter — it has
**no serializer**: it exposes the mutable lxml tree and callers serialize it
themselves.

## Commands

- `uv sync` — install runtime and dev dependencies.
- `uv run pytest` — run the default test suite.
- `uv run pytest tests/test_binding.py::test_load_tool_returns_document` — run a single test.
- `uv run pytest -m slow` — run the xsdata codegen sweep over every vendored XSD.
- `uv run ruff check .` / `uv run ruff format .` — lint / format.
- `uv run mypy src` — type-check (strict).
- `uv run python scripts/fetch_schemas.py` — download release XSDs (`--force` re-downloads all).
- `uv run python scripts/regenerate.py` — regenerate the typed models from the latest XSD.
- `uv run galaxy-tool-xml validate <file>` / `suggest <file>` / `profiles` — the CLI.
- `uv build` — build the wheel.

## Naming

The repo directory, the distribution, and the CLI command are all
`galaxy-tool-xml`; the import package is `galaxy_tool_xml`.

## Architecture

`ToolDocument` (`document.py`) wraps a mutable lxml tree — the **source of
truth**, faithfully preserving CDATA, comments, and attribute order.
`binding.py` parses (`load_tool`, `parse_tool`) and validates (`validate_tool`).
`profiles.py` resolves a tool's `profile` to one of the ~27 vendored per-release
XSDs. `macros.py` handles Galaxy macros and is the sole `galaxy-util` adapter.
`corrections.py` suggests near-miss typo fixes. `models/` is the
xsdata-generated read-only typed view, reached via `ToolDocument.model()`.

The public API is the prose-declared list in `README.md`; everything else is
private and may change.

## Coding standards

Hand-written code follows **dignified-python**, vendored at
`.claude/skills/dignified-python/`: LBYL over `try/except`; exceptions only at
the click error boundary (chained `from e`); `pathlib` with explicit
`encoding` for text I/O; no import-time side effects (`@cache` for module
state); absolute imports, no re-exports, no `__all__`; keyword-only arguments
after the first. `optimized-python` (`.claude/skills/optimized-python/`) is
installed as a reference; **dignified-python governs on any conflict**. The
xsdata-generated `models/` is exempt — it is not hand-written.

## Non-obvious conventions

- The lxml tree is the source of truth; the typed `Tool` model is a derived
  read-only view. The library does not emit XML.
- Parsing uses `strip_cdata=False`: CDATA, comments, and attribute order are
  preserved. XML is parsed from `bytes`, never a decoded `str`, so the
  document's own encoding declaration is honoured.
- The Galaxy XSD is a **post-macro-expansion** schema; `validate_tool`
  transforms the tool per `macro_handling` (default `expand`) into a throwaway
  copy and validates that — the `ToolDocument` tree is never mutated.
- `galaxy.util` is Galaxy's *internal* API; all use of it is confined to
  `macros.py`, and `galaxy-util` is pinned to a version range.
- `models/` is xsdata-generated — never hand-edit; regenerate via
  `scripts/regenerate.py`. It is excluded from ruff and mypy, and its
  re-exporting `__init__.py` is the one sanctioned exception to the
  no-re-exports rule.
- `schema/` holds vendored XSDs downloaded once by `scripts/fetch_schemas.py`;
  re-running is additive, `--force` re-downloads. `manifest.json` and
  `PROVENANCE.md` are committed alongside the XSDs.
- Validation is profile-aware (per-release XSD); binding is not — one `Tool`
  model from the latest XSD, parsed with a lenient xsdata config.
- `corrections.py` is suggest-only and independent of `validate_tool`; its
  vocabulary comes from introspecting the generated `models/`, with a macro
  skip-set so an un-expanded tool's macro constructs are never flagged.
- No-profile tools validate against the latest XSD — a deliberate divergence
  from Galaxy, which defaults a missing `profile` to `16.01`.
- Failure modes: syntax errors (`load_tool` raises, the others collect them),
  macro-expansion errors, and XSD validation errors. The XSD has no
  `targetNamespace`, so Galaxy tool XML is namespace-free.

## Implementation workarounds

Two deviations from a naive implementation, both forced by upstream bugs:

- **xsdata codegen** (`scripts/regenerate.py`): xsdata 26.2's circular-reference
  detector raises `KeyError` on the Galaxy 24.2+ schema when inner classes are
  nested, so codegen sets `output.unnest_classes = True`.
- **Schema compilation** (`profiles.py`): Galaxy releases 19.05 through 23.0
  shipped an XSD whose `Output` type has a non-deterministic content model that
  libxml2 refuses to compile. `compiled_schema` retries after applying Galaxy's
  own release-23.1 fix (drop the redundant `Output` group) in memory — the
  vendored XSD files on disk remain verbatim.
