# Decisions and Assumptions

A maintainer-facing record of every non-obvious assumption this project
relies on and every design decision driven by data, by an upstream
constraint, or by an explicit user preference. Live document — extend
when new evidence arrives or an assumption changes.

The narrative architecture lives elsewhere: `CLAUDE.md` (current state),
`README.md` (public API), `docs/per-version-models-plan.md` (the
per-version model refactor), `docs/codemod-architecture.md` (tier-2
design). This file is the **why** for the choices those docs reflect.

Each entry should answer: *what we assume / chose · what the alternative
was · what evidence or constraint settled it*.

---

## 1. Galaxy ecosystem (verified during planning)

These are external facts about the Galaxy project that the library
treats as given. Any change here is a real-world ecosystem change, not a
refactor.

| # | Assumption | Verified |
|---|---|---|
| 1.1 | The Galaxy tool XSD lives at `lib/galaxy/tool_util/xsd/galaxy.xsd`; releases before ~`20.09` shipped it at `lib/galaxy/tools/xsd/galaxy.xsd`. The old path is gone by `release_21.05`. | `scripts/fetch_schemas.py` tries the new path then the old; see `docs/per-version-models-plan.md` and `schema/PROVENANCE.md`. |
| 1.2 | The XSD is self-contained (no `xs:import` / `xs:include`), declares **no** `targetNamespace`, root element `tool`, strict content models (`xs:all` / `xs:choice` of named elements, no wildcards). | Inspected at planning time; the namespace-free property is exposed to consumers via the no-namespace lxml tree. |
| 1.3 | The XSD is **post-macro-expansion**: it does not define `<expand>`; it defines `<macros>` / `<import>` / `<token>` / `<xml>` / `<macro>` (the definition side only). Galaxy validates tools against the XSD **only after expanding macros**. | Confirmed against `tool_util/linters/xsd.py` in galaxyproject/galaxy. Drives the `macro_handling="expand"` default for `validate_tool`. |
| 1.4 | Galaxy ships `release_*` branches (`release_13.01` … `release_26.1` as of 2026-05-27 — 28 of those branches ship the XSD); the suffix matches the `profile` attribute value. Not every branch ships the XSD. | `git/matching-refs/heads/release_` API call inside `scripts/fetch_schemas.py`. |
| 1.5 | A missing `profile` attribute defaults to `"16.01"` *inside Galaxy*. **This project deliberately diverges**: a no-profile tool validates against the latest XSD (user choice). | Galaxy source + planning decision in `PLAN.md`. |
| 1.6 | A tool's set of valid profiles is **not guaranteed to be contiguous** across vendored XSDs. | Corpus sweep observation — see §10. `newest_valid_profile` does a linear newest→oldest scan rather than a binary search, on the strength of this. |
| 1.7 | `galaxy.util.xml_macros.load_with_references(path) -> (ElementTree, imported_paths)` is the canonical macro expander; it handles `<import>`, nested `<token>`, `<expand>` / `<macro>` / `<xml>`, and parameterised `<yield>` macros. Path-based, not in-memory. | Mirrored 1:1 in `macros.py`'s `expand_from_path` / `expand_from_tree`. |
| 1.8 | `galaxy.util` is Galaxy's **internal** API — not a stability-guaranteed surface. The `galaxy-util` PyPI package is CalVer (currently `26.0`). | Confirmed against the project's own docs / release cadence. Drives the range pin (§2.4) and the macros.py isolation rule. |
| 1.9 | The Galaxy ToolShed exposes content only over the **Mercurial wire protocol** — no tarball or raw-file endpoint on the public API. The API itself is HTTP/JSON, but file fetches require `hg clone`. | `scripts/fetch_toolshed.py` necessarily uses `hg`. |
| 1.10 | A ToolShed repository's "version" is its tip changeset; the API does **not** list it on the repository envelope. It must be captured client-side via `hg id -i` after cloning. | Necessitated the manifest in `corpus/galaxy-toolshed/manifest.json`. |

---

## 2. Python ecosystem dependencies

For each runtime dependency: pin, why, and what would break if a
maintainer "tightened it up" naively.

### 2.1 `xsdata[lxml] >= 26.2` — typed-model codegen + runtime parser

- Build-time and runtime dependency.
- 26.2 is the floor because the project drives codegen via the **API**
  (`ResourceTransformer(config=GeneratorConfig()).process([uri])`) — the
  signature settled at 26.2 (no `print` argument; positional list of
  `file://` URIs).
- See §5 for the codegen workarounds.

### 2.2 `lxml >= 5` — declared explicitly

- `galaxy-util` needs lxml for its XML code path but **does not
  declare** it. Without our explicit declaration, `pip install
  galaxy-tool-xml` could break on a fresh env where neither dep pulled
  lxml in transitively.

### 2.3 `click >= 8` — CLI

- Standard pin. No workarounds.

### 2.4 `galaxy-util >= 24, < 27` — macro expansion, range-pinned

- Range, not floor, because `galaxy.util` is Galaxy's internal API
  (Assumption 1.8). A wider open range would let a new Galaxy release
  silently change the macro semantics underneath us.
- All `galaxy.util` use is confined to `src/galaxy_tool_xml/macros.py`,
  so the blast radius of a future incompatibility is one module.
- Widen the upper bound only after `test_macros.py` runs green against
  the candidate version.

### 2.5 `packaging >= 23` — declared explicitly

- `packaging` is only a *transitive* dep of `galaxy-util`. Our use of
  `packaging.version.Version` in `profiles.py` is a direct dependency
  and must be declared as such, so a `galaxy-util` minor release that
  drops the transitive doesn't break us.

### 2.6 Dev: `xsdata[cli]`, `pytest`, `ruff`, `mypy`, `mercurial`

- `xsdata[cli]` brings the codegen toolchain (`ResourceTransformer` and
  its deps, e.g. `toposort`) — required at build time too (see
  `[build-system].requires`).
- `mercurial` is invoked only by `scripts/fetch_toolshed.py`; the
  pip-installed package brings its own `hg` binary, so the script does
  **not** rely on a system Mercurial install.

---

## 3. Representation: lxml tree is the source of truth

| Decision | Alternative | Why |
|---|---|---|
| **Mutable lxml tree** = source of truth; xsdata model = derived read-only view via `ToolDocument.model()`. | An xsdata-only representation, mutated through typed setters. | xsdata dataclasses cannot hold XML comments or preserve attribute order, so they cannot be the faithful representation. The downstream formatter (tier 3) needs that fidelity. |
| **No serializer in the library.** Callers serialise the tree themselves. | Provide a default `ToolDocument.write()`. | Tier 3 (`galaxy-tool-fmt`) is the only thing that writes Galaxy XML to disk in the planned three-tier architecture; baking a formatter into tier 1 would prejudge that. |
| **Parsing uses `strip_cdata=False` and reads `bytes`, never `str`.** | Decode to `str` first, or let CDATA collapse to text. | CDATA inside `<command>` / `<help>` carries shell scripting; collapsing it would silently change tool behaviour. Reading bytes lets lxml honour the XML encoding declaration. |
| **Library modules never call `logging.basicConfig`.** Each uses `logging.getLogger(__name__)`; the CLI installs the handler. | Configure at import time. | Library import must have no side effects (dignified-python). |

---

## 4. Profile-aware validation and binding

| Decision | Why |
|---|---|
| **Per-release vendored XSD** (~28 files, committed under `src/galaxy_tool_xml/schema/`). | The XSD evolves across releases; validating an old tool against the newest schema is misleading. |
| **`validate_tool` resolves the XSD from the tool's `profile`** with `on_missing="nearest"` as default. `exact` and `latest` are the other modes. | Real-world tools declare any profile in the release range; "nearest" mirrors what Galaxy itself accepts. |
| **`macro_handling` defaults to `"expand"`** (modes: `off` / `skip` / `strip` / `expand`). | Galaxy validates the post-expansion tool (Assumption 1.3); the default matches what Galaxy actually does. |
| **`expand` writes a throwaway temp copy** — the `ToolDocument.tree` is **never** mutated by validation. | The formatter contract requires the tree to be untouched after parse; loss of comments / whitespace in the expanded tree doesn't matter because the tree is discarded after the validation call. |
| **`newest_valid_profile` is a linear newest→oldest scan.** | Validity is not contiguous (Assumption 1.6); a valid/invalid probe at a single profile cannot distinguish "too old" from "too new", so binary search would be wrong. O(1) common case (modern tool validates at latest); worst case is one validation per vendored profile (28 at time of writing), and `compiled_schema` is `@cache`d. |
| **No-profile tools validate against the latest XSD.** | Deliberate divergence from Galaxy's `"16.01"` default — explicit user choice in `PLAN.md`. |
| **Binding (`ToolDocument.model()`) is profile-aware too.** A tool's tree is bound against the model for its resolved profile, overridable via `model(version=...)`. | The downstream codemod tool needs faithful typed views of *each* release (see `docs/per-version-models-plan.md`). |
| **xsdata `ParserConfig` is lenient** (`fail_on_unknown_properties=False`, `fail_on_unknown_attributes=False`); schema-required fields the tree omits default to `None`. | Lets an un-expanded tool bind without raising; macro tokens absent from the post-expansion schema don't crash binding. |

---

## 5. Implementation workarounds (forced by upstream bugs)

These are **not** preferences — each is a specific upstream defect with
a recorded mitigation. Each should be revisited when the upstream
project releases a fix.

### 5.1 xsdata 26.2 circular-reference detector — `KeyError` on Galaxy 24.2+

- **Symptom:** xsdata's nested-class circular-reference detector raises
  `KeyError` on the Galaxy 24.2+ schema when inner classes are nested.
- **Mitigation:** `_codegen.py` sets
  `GeneratorConfig.output.unnest_classes = True`. Each XSD version is
  generated in its own subprocess because xsdata caches the resolved
  output path process-wide.
- **Where:** `src/galaxy_tool_xml/_codegen.py`; the flag is unconditional
  (harmless on older XSDs).
- **Side effect:** The generated class taxonomy is flat top-level
  (`Param`, `Conditional`, `ChangeFormatWhen`, etc.) — see `docs/codemod-architecture.md` §"Node taxonomy".

### 5.2 libxml2 — non-deterministic content model on Galaxy 19.05–23.0

- **Symptom:** Galaxy releases 19.05 through 23.0 shipped an XSD whose
  `Output` type has a non-deterministic content model; libxml2 refuses
  to compile it.
- **Mitigation:** `profiles.compiled_schema` retries after applying
  Galaxy's own release-23.1 fix (drop the redundant `Output` group) in
  memory. The vendored XSD files on disk remain verbatim.
- **Where:** `src/galaxy_tool_xml/profiles.py`.

---

## 6. Corpus stats system

The maintainer-facing corpus sweep
(`scripts/corpus_check.py`) and its supporting docs make several
non-obvious choices, several of them recently revisited.

| Decision | Why |
|---|---|
| **Two sources: `github` and `toolshed`; a `combined` sweep deduplicates by sha256 of file bytes.** | github captures the curated repositories; toolshed captures the long tail. Identical bytes is a stronger dedup signal than path or `@id` matching across repos. |
| **A "tool" is a file whose XML root element is literally `<tool>`.** Other XML files (tool_conf, repository_dependencies, etc.) are filtered out of stats and out of the fine-grained data file. | The library's domain is `<tool>` files; counting anything else would distort every distribution. |
| **`combined` mode dedup affects stats counters and invariant checks only; the fine-grained data file emits *all occurrences*.** | The Sources table in the markdown counts how many duplicates were *dropped from the aggregate stats*; the fine-grained data answers "which repo/path actually has this tool", and that's a per-occurrence question. |
| **Toolshed `version` = tip changeset captured via `hg id -i` before `.hg/` is removed.** Recorded in a top-level manifest `corpus/galaxy-toolshed/manifest.json`; missing entries default to `"unknown"`. | The ToolShed API doesn't expose tip changesets (Assumption 1.10); `.hg/` is removed to save ~80% disk. The manifest is the single source of truth and is gitignored (the corpus itself is). |
| **`tool_id` column = post-macro-expansion `@id`**, falling back to the raw `@id` (often a macro-token string) on expansion failure. | Driven by the 2000-tool survey (§10.1) — `@id` is present on 100% of tools and is the tool's logical identity, distinct from its file path. The expansion is free because `_expanded_attrs` already expands once and reads both `profile` and `id` from the result. |
| **Fine-grained schema = (`repo`, `version`, `path`, `tool_id`, `sha256`)** for per-source files; **plus** (`profile_raw`, `profile_expanded`, `newest_valid`) and one `valid_<profile>` 0/1 flag per vendored profile (28 columns at time of writing) for the combined file. | User choice: keep per-source files minimal and put the cross-source profile analysis in the combined view. The per-profile validity flags expose the full validity vector so downstream consumers can reason about non-contiguity (§10.3) without re-running validation. |
| **JSON + TSV, both formats every run.** JSON preserves schema column order via `dict` insertion order; TSV sanitises `\t` / `\n` / `\r` → space defensively. | Pandas / duckdb consume JSON; jq / awk / spreadsheets consume TSV. Both are tiny to write and worth shipping together. |
| **Fine-grained data and markdown stats share one gate** (skipped on `--no-stats`, `--limit`, `--repo`). | Partial sweeps must never produce a truncated artifact. |
| **Combined-mode duplicate rows reuse the first-seen `ToolStats` from a sha256→stats cache.** | Re-running `_exercise` on every duplicate would multiply the sweep time by ~2× without adding information (same bytes → same validity vector). |
| **Tooling: `hg` and `git` binaries via subprocess; `urllib` for the GitHub REST API.** | Each script makes 2–3 calls per repo and the network dominates everything. PyGithub / GitPython / python-hglib would be heavyweight imports with no measurable benefit; the Mercurial Python API is explicitly *not* a stable surface. |
| **Per-tool failure reasons categorized and surfaced in the combined stats markdown only.** Two new sections — *Macro-expansion failure reasons* (Group A) and *Tools with no valid vendored profile — reason breakdown* (Group A+B) — appear in `docs/combined_corpus_stats.md`. Tool-level reason fields (`expansion_failure_reason`, `no_valid_reason`) live on `ToolStats` but are not exposed in the fine-grained data files. | The aggregate breakdown answers "are these our bugs?" at a glance (the answer is *no* — see §10.4). The combined view is the right place because the breakdown is most informative when deduplicated across sources. Categorization runs only when needed (no-valid tools get one extra `validate_tool` call to pull the first error). |

---

## 7. Tooling and packaging

| Decision | Why |
|---|---|
| `uv` as project manager, `hatchling` as build backend with a custom build hook. | The build hook generates one per-version model package per vendored XSD (28 at time of writing) at wheel and editable-install time (`docs/per-version-models-plan.md`); `uv_build` had no hook surface, so the backend moved. |
| `ruff` lint + format; `mypy --strict`. Both exclude the generated `models/v*/` and `models/any_tool.py`. | Hand-written code is held to the standard; generated code isn't ours to fix. |
| No CI (`v0.1`, deliberate). | A single maintainer + a fast local `pytest`/`ruff`/`mypy` triad is sufficient at this scale; CI is a later add. |
| `.gitignore` excludes `src/galaxy_tool_xml/models/v*/`, `…/any_tool.py`, and the whole `corpus/` tree. Committed-but-generated: nothing in models; the `schema/` XSDs are committed. | Generated code regenerates deterministically from the committed XSDs + pinned xsdata; the corpus is reproducible from `corpus_sources.json` + the toolshed manifest. |

---

## 8. Coding standards

**`dignified-python` governs** (vendored at `.claude/skills/dignified-python/`); `optimized-python` is installed
as a reference. On conflict, dignified-python wins. Key applications in
this repo:

- LBYL over `try/except`. Exceptions only at the click error boundary
  (chained `from e`) and where third-party APIs offer no LBYL form.
- `pathlib.Path` with explicit `encoding="utf-8"` on every `read_text` /
  `write_text`.
- No import-time I/O; `@functools.cache` for module-state accessors
  (`_corpus_sources`, `_toolshed_manifest`, `compiled_schema`, etc.).
- Absolute imports, no re-exports, no `__all__`. Exception (sanctioned):
  the xsdata-generated `models/v*/__init__.py` re-exports its module so
  `from galaxy_tool_xml.models.v26_0 import Tool` works.
- Keyword-only arguments after the first.
- Hand-written code is checked by ruff + mypy; the generated `models/`
  is excluded from both.

---

## 9. Three-tier vision (context)

`galaxy-tool-xml` is tier 1 of a planned three-package architecture:

| Tier | Package | Status | Job |
|---|---|---|---|
| 1 | `galaxy-tool-xml` | this repo | parse · validate · typed view; no serializer |
| 2 | `galaxy-tool-codemod` (name TBD) | designed, not built | LibCST-shaped structural refactors |
| 3 | `galaxy-tool-fmt` (name TBD) | designed, not built | `black`-like opinionated formatter; the only thing that writes XML |

This split is *why* tier 1 has no serializer (Decision 3) and why it
ships full per-version typed models (Decision 4): the converter in
tier 2 needs a faithful view of *each* release to plan structural
edits, and tier 3 owns trivia preservation downstream.

Full design: `docs/codemod-architecture.md`.

---

## 10. Testing-derived measurements

Each measurement records what was sampled, when, and what decision it
informed. Add new measurements here when a new survey runs.

### 10.1 Tool `@id` vs. path (2026-05-27)

Survey to decide whether the fine-grained corpus data should carry
`path`, `@id`, or both as the tool-identity column.

| Property | Result |
|---|---|
| Files scanned | 3,375 random XML files; first 2,000 `<tool>` roots kept |
| `@id` present on `<tool>` | 100.0% (2,000 / 2,000) |
| `@id` contains a macro token (e.g. `@PROFILE@`) | 0.8% (16 / 2,000) — all from a small number of macro-heavy families (iuc/bcftools, iuc/gemini, dimet, b2b) |
| `@id` matches the file stem | 51.6% |
| `@id` matches the parent directory name | 37.5% |
| `<tool>` files with no `@id` | 0 |

**Conclusion:** path and `@id` agree only ~52% of the time, so they
carry distinct information. We emit **both columns** in the corpus data
files (§6). Expansion of macro-token `@id`s is free because the sweep
already calls `expand_from_path` for `profile_expanded`.

### 10.2 Corpus size and source mix (2026-05-27 sweep)

Snapshot of the corpus as observed by a full `corpus_check.py` run.

| Measure | github | toolshed | combined |
|---|---:|---:|---:|
| Repositories swept | 21 | 7,651 | 7,672 |
| `<tool>` files kept | 4,190 | 8,675 | 9,414 unique + 3,451 duplicate occurrences (12,865 rows) |
| Aggregate "duplicates dropped" (incl. non-tool XMLs) | — | — | 8,077 |
| Total XML files iterated | — | — | 21,617 |

The gap between "8,077 duplicates dropped" and "3,451 duplicate tool
rows in the data file" (= 4,626) is the count of duplicates of
**non-tool** XML files (tool_conf.xml, repository_dependencies.xml,
etc.); those never enter the data file at all because they're not
tools.

**Conclusion:** the dedup semantics in the combined markdown (counts
every duplicate XML file) and in the fine-grained data file (only tool
occurrences) are correct and reconcile arithmetically.

### 10.3 Validity-vector contiguity

Observation (recorded in `docs/per-version-models-plan.md` §"Risks &
assumptions" and confirmed in every sweep since): a non-trivial
fraction of real tools have a **non-contiguous** validity vector — they
validate at some profile, fail at an intermediate profile, and validate
again later.

| Sweep | Date | Non-contiguous | Total tools | % |
|---|---|---:|---:|---:|
| github | 2026-05-27 | 162 | 4,190 | 3.9% |
| toolshed | 2026-05-27 | 215 | 8,675 | 2.5% |
| combined | 2026-05-27 | 207 | 9,414 | 2.2% |

**Conclusion:** Assumption 1.6 holds across corpora at >2%. The
`newest_valid_profile` implementation must remain a linear newest-first
scan (Decision 4) — a binary search would be unsound.

### 10.4 No-valid-profile taxonomy (2026-05-27 combined sweep)

Of the 9,410 unique tools, **761** (8.1%) do not validate against any
of the 28 vendored XSDs. Investigation showed every category traces to
a genuine schema-noncompliant property of the tool, not a library bug.

**Group A — macro expansion failed** (17 tools / 2.2% of the no-valid
set): the post-expansion tree never reaches the XSD.

| Reason | Count |
|---|---:|
| undefined macro reference in `<expand>` | 8 |
| imported `macros.xml` file not on disk | 6 |
| malformed XML in tool file (e.g. `--` inside a comment, unmatched tags) | 3 |

**Group B — expansion ok, XSD rejects everywhere** (744 tools / 97.8%):

| Reason | Count | % of no-valid |
|---|---:|---:|
| XSD does not declare attribute used by tool | 351 | 46.1% |
| XSD does not allow element under this parent | 220 | 28.9% |
| XSD does not allow element at all | 37 | 4.9% |
| attribute value outside XSD's enumeration | 35 | 4.6% |
| other XML syntax error (recovered tree, parser logged errors) | 35 | 4.6% |
| invalid boolean (`"True"`/`"False"` vs `"true"`/`"false"`) | 33 | 4.3% |
| other XSD type / pattern mismatch | 19 | 2.5% |
| XSD-required attribute missing | 10 | 1.3% |
| invalid character encoding (non-UTF-8 bytes) | 4 | 0.5% |

**Conclusion:** ~75% of all no-valid tools (B1 + B2 = 571) use
attributes or elements the public Galaxy XSD does not formally cover —
a long-standing gap between Galaxy's runtime parser (lenient) and its
public schema (strict). The remaining ~25% split between minor type
mismatches (booleans, enums, regex facets) and outright malformed
input (syntax errors, encoding errors, missing imports). Each category
is now surfaced in `docs/combined_corpus_stats.md` so future drift is
immediately visible.

---

## 11. Open items

- **`source` column in the combined corpus data file.** Today the
  source is implicit (toolshed repos carry `/` in `repo`, github repos
  don't). An explicit `source` column is a small future addition if
  downstream consumers want it.
- **CI** — out of scope for v0.1 (§7).
- **Schema-error line numbers in `expand` / `strip` modes** point to
  the transformed tree, not the original source file. Only
  `macro_handling="off"` yields original-source line numbers. Inherent
  to validating a post-transformation tree.
- **Typo suggestions are now profile-bound** (via `corrections.py`'s
  profile-aware vocabulary, per `docs/per-version-models-plan.md` §7).
  A historical caveat in `PLAN.md` ("uses the latest schema's
  vocabulary") is no longer applicable.
