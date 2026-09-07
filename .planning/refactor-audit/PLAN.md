# Refactor audit: redundancy, mergeable files, scattered configuration

## Summary

A survey along the three axes asked for. Findings are ordered by
severity × cheapness, and separated from the things that *look* like smells but are
not — the second list matters, because acting on it would make the codebase worse.

Every claim below is measured, not eyeballed. Where a name appears in several
modules I compared the **values**, because a shared name is not duplication.

---

## A. Scattered configuration

### A1. `pytest.ini` and `pyproject.toml` both configure pytest — and the comment is backwards ⚠️

**Severity: high, effort: trivial.** This is the one live trap.

```
configfile: pytest.ini (WARNING: ignoring pytest config in pyproject.toml!)
```

pytest reports that on every run. Meanwhile `pytest.ini` line 2 says:

> `# Source of truth is pyproject.toml [tool.pytest.ini_options]; mirror changes there.`

So the file that *wins* instructs you to edit the file that is *ignored*. Anyone
adding a marker to `pyproject.toml` — the natural place, and the one the comment
points at — gets `--strict-markers` failures and no clue why.

They are not even equivalent. `pytest.ini` carries four keys `pyproject.toml`
lacks: `testpaths`, `python_files`, `python_classes`, `python_functions`. Deleting
`pytest.ini` on the comment's advice would silently widen collection from `tests/`
to the whole repo.

- **Smell:** Duplicated Code + a comment that lies.
- **Refactoring:** **Consolidate Duplicate Conditional Fragments** — pick one home.
- **DONE — consolidated into `pyproject.toml`**, not `pytest.ini`. That is the
  direction the audit called riskier, so it was done with a measured before/after:
  every shared key was proven byte-identical first, the four discovery keys were
  carried over explicitly, and collection is unchanged at **5234**. `configfile`
  now reports `pyproject.toml` and the "ignoring" warning is gone. Consolidating
  *there* rather than into `pytest.ini` was the right call because ruff, mypy,
  coverage and black already live in `pyproject.toml` — which is the actual answer
  to "scattered config".
- **Verification:** `pytest --collect-only -q` still reports 5234 tests and no
  `ignoring pytest config` warning; marker list unchanged.

### A2. `FS_FILES_PATH` / `FS_DIRS_PATH` defined twice inside the same package

Same values, two modules in one package tree:

- `src/back/core/databricks/constants.py`
- `src/back/core/databricks/uc/constants.py`

- **Smell:** Duplicated Code.
- **Refactoring:** **Move Field** — keep the `uc/` copy (the Files API is a UC
  concern), delete from the parent, re-export from `databricks/__init__.py` if
  anything imports the parent path.
- **Effort:** minutes.

### A3. Config files that are *correctly* separate — checked and left alone

| Pair | Why it is not duplication |
|---|---|
| `AGENTS.md` / `CLAUDE.md` | Both are thin pointer files (16 and 61 lines) to the same canonical trio, and both say so explicitly. Two agents need two entry points; the rules live in one place. |
| `ci/coverage_thresholds.yaml` / `[tool.coverage]` | Different jobs: per-package CI gates read by `ci/check_coverage.py` vs. coverage-tool configuration. |
| `tests/eval/thresholds.yaml` | Per-agent eval thresholds. Unrelated to coverage. |
| 12 × `constants.py` | Package-scoped constants are the intended pattern (`.coding_rules.md` §3). Only A2 is a genuine overlap. |
| `src/mcp-server/pyproject.toml` | A separate deployable with its own lock. |

---

## B. Real code redundancy

### B1. The four `agent_mapping_pge` modules share a copy-pasted ReAct loop

Measured line-level similarity (`difflib.SequenceMatcher` over lines):

| Pair | Similarity |
|---|---|
| `entity.py` ↔ `relationship.py` | **54.3%** |
| `planner.py` ↔ `critic.py` | 48.8% |
| `critic.py` ↔ `entity.py` | 48.5% |
| `critic.py` ↔ `relationship.py` | 47.4% |
| `planner.py` ↔ `entity.py` | 46.0% |
| `planner.py` ↔ `relationship.py` | 43.3% |

Between `entity.py` (806 LOC) and `relationship.py` (864 LOC) alone there are **13
identical runs longer than 8 lines, totalling 192 identical lines**. The shared
body is the tool-call dispatch cycle: read `tool_calls`, log the names, append the
message, `json.loads` the arguments with a `JSONDecodeError` fallback, dispatch,
append a `tool_result` step, check for terminal success.

- **Smell:** Duplicated Code, four ways.
- **Refactoring:** **Form Template Method** — the variation between the four is the
  system prompt, the tool set, the terminal condition and the progress message.
  Extract the loop into `agents/engine_base.py` (which already owns
  `call_chat_completion` and `dispatch_tool`) as something like
  `run_react_loop(target, messages, tools, handlers, *, is_terminal, on_step, …)`,
  and let each module supply its four differences.
- **Effort:** substantial — this is the largest single win available, and the
  riskiest. It must be its own commit series with the suite green between chunks.
- **Order:** do it *after* A1/A2/B2, and only with the four modules' existing tests
  as the safety net (`tests/agents/agent_mapping_pge/` — 52 tests today).

### B2. `_MAX_TOKENS` duplicated four times with one value

`planner.py`, `critic.py`, `entity.py`, `relationship.py` — same package, identical
value.

- **Refactoring:** **Replace Magic Number with Symbolic Constant**, hoisted to
  `agents/agent_mapping_pge/__init__.py` or a package `constants.py`.
- **Effort:** minutes. Do this one first — it is a five-line down-payment on B1 and
  proves the package has somewhere shared to put things.

### B3. `RDF_TYPE` / `RDFS_LABEL` — only half of it is fixable

Four definitions, identical values. But two of them are in **separate
deployables**:

| Site | Can import `back.core`? |
|---|---|
| `src/back/core/graphdb/constants.py` | — (the home) |
| `src/agents/tools/graph_formatting.py` | **yes** → real duplication, merge it |
| `src/jobs/graph_analytics_job.py` | **no** — 0 `back.*` imports; a Spark job |
| `src/mcp-server/server/app.py` | **no** — own `pyproject.toml` + lock |

- **Refactoring:** **Move Field** for the `agents/tools` copy only. Leave the other
  two and add a one-line comment in each pointing at the canonical home, so the
  next reader knows the copy is deliberate rather than sloppy.

---

## C. Looks like a smell, is not — do not "fix" these

Listed because a naive pass would break them.

| Candidate | Measurement | Verdict |
|---|---|---|
| `MAX_ITERATIONS` × 13 | **7 distinct values** (6, 10, 12, 20, 50, 60, …) | Per-agent tuning. Hoisting it would silently change every agent's loop budget. |
| `LLM_TIMEOUT` × 13 | 2 values (120, 180) | Per-agent. |
| `SYSTEM_PROMPT` × 11, `_TRACE_NAME` × 12 | necessarily different | Must differ per agent; the trace name is how spans are told apart. |
| `_HTTP_TIMEOUT` × 3, `_ITERATION_DELAY_SEC` × 5, `_REQUEST_TIMEOUT` × 2, `MAX_DEPTH` × 2 | 2 distinct values each | Per-site. |
| 5 × 12-line files in `back/core/errors/` | one public class each | **The project's own rule** (`.coding_rules.md` §2: one public class per file). Merging them would violate it. |
| `src/api/service.py` (33 lines, pure re-exports) | patched by name in 3 test modules; documented as the stable facade for `api.routers.v1` | An intentional seam, not a legacy god-module. Deleting it forces tests to patch deeper into `back/`. |
| 12 single-module packages (`back/core/logging`, `w3c/rdfs`, …) | — | Room to grow, and the `__init__.py` re-export contract depends on the package boundary. |

---

## D. Out of scope here, but worth naming: 12 files over the 800-LOC ceiling

`.coding_rules.md` §9 sets ~800 LOC as the trigger for **Extract Class** and already
names `DigitalTwin.py` as the canonical example.

| File | LOC |
|---|---|
| `back/objects/domain/SettingsService.py` | 4354 |
| `back/objects/registry/store/postgres/store.py` | 4107 |
| `back/objects/digitaltwin/DigitalTwin.py` | 3516 |
| `api/routers/internal/dtwin.py` | 2798 |
| `back/core/w3c/sparql/SparqlTranslator.py` | 2408 |
| `api/routers/internal/ontology.py` | 2399 |
| `back/objects/ontology/Ontology.py` | 2329 |
| `back/objects/mapping/Mapping.py` | 2224 |
| `back/objects/domain/Domain.py` | 2103 |
| `api/routers/digitaltwin.py` | 1703 |
| `api/routers/internal/settings.py` | 1636 |
| `back/objects/session/DomainSession.py` | 1598 |

This is a programme of work, not a cleanup, and every one of them is pre-existing.
Splitting `SettingsService.py` alone is a multi-day sequence with real regression
risk. It should be planned per file, driven by a concrete need (a feature landing in
that file), rather than as a bulk tidy.

---

## Recommended order

| # | Item | Effort | Risk |
|---|---|---|---|
| 1 | **A1** — one pytest config, fix the lying comment | minutes | none |
| 2 | **B2** — hoist `_MAX_TOKENS` | minutes | none |
| 3 | **A2** — de-duplicate `FS_*_PATH` | minutes | low |
| 4 | **B3** — merge the one mergeable `RDF_TYPE`/`RDFS_LABEL` copy; comment the two structural ones | minutes | low |
| 5 | **B1** — Form Template Method on the four PGE loops | days | **high** — own commit series, suite green between chunks |
| 6 | **D** — Extract Class on the oversized files | weeks | high — plan per file, driven by need |

Items 1–4 are ~30 minutes total and carry no behavioural risk. Item 5 is the real
prize and deserves its own plan. Item 6 should not be started as a tidy-up.

## Verification for items 1–4

- `uv run --frozen pytest -q -m "not scenario"` → 4930 passed, unchanged.
- `pytest --collect-only -q` reports no `ignoring pytest config` warning and the
  same 5234 collected.
- ruff F-rule findings unchanged at 67.
- Constant values byte-identical before/after (assert with the same AST scan used to
  produce this audit).


---

# Applied — items 1–4 (plus two found while doing them)

| # | Item | Outcome |
|---|---|---|
| 1 | **A1** pytest config | `pytest.ini` deleted; `[tool.pytest.ini_options]` completed with `testpaths`, `python_files`, `python_classes`, `python_functions`. Collection unchanged at 5234; `configfile: pyproject.toml`; warning gone. |
| 1b | **`--strict-markers` never actually worked** | Found while verifying 1. See below. |
| 2 | **B2** `_MAX_TOKENS` × 4 | New `agents/agent_mapping_pge/constants.py` holds `MAX_TOKENS`. The canonical rationale comment moved with it; the three "See planner._MAX_TOKENS comment" cross-references are gone. Per-stage knobs verified still distinct — `MAX_ITERATIONS` is still `[50, 6, 12, 12]`. |
| 3 | **A2** `FS_*_PATH` × 2 | `uc/constants.py` now re-exports from the parent instead of re-deriving from the same `API_PREFIX`. All three import paths verified to yield identical values. |
| 4 | **B3** `RDF_TYPE`/`RDFS_LABEL` | The one mergeable copy (`agents/tools/graph_formatting.py`) now imports from `back/core/graphdb/constants.py`. The two in `src/jobs/` and `src/mcp-server/` are labelled as deliberate, with the reason. |
| 5 | **`ROLE_*` × 2** | Found while re-running the scan. See below. |
| 6 | **`_SAFE_SCHEMA_RE` × 2** | Investigated, deliberately **not** merged. See below. |

## 1b. `--strict-markers` has never been enforced, despite the docs saying it is

`.coding_rules.md` §10 states *"`--strict-markers` is enforced."* It was not.

`addopts` does carry `--strict-markers` (and `-v` from the same string demonstrably
works), but the flag installs a warning filter promoting `PytestUnknownMarkWarning`
to an error, and the ini's own `filterwarnings` list is applied **afterwards** and
outranks it. Net effect: an undeclared marker only ever warned.

Confirmed pre-existing, not caused by the consolidation — identical behaviour with
`pytest.ini` restored. Confirmed the flag itself is fine: passed explicitly on the
command line it errors correctly.

Fixed by re-asserting `"error::pytest.PytestUnknownMarkWarning"` as the last
`filterwarnings` entry. **Verified a no-op first** — the full suite passes 4930
with the filter active, so nothing today relies on the loophole — and then verified
it bites: an undeclared marker is now a collection error.

## 5. `ROLE_ADMIN` / `ROLE_APP_USER` / `ROLE_NONE` declared twice

`PermissionService.py` and `AppRoleService.py` each declared them. Same package,
same values, and they are compared **across** the boundary — `AppRoleService.resolve_role`
returns one and `PermissionService` tests it by equality, never against a shared
list. A rename or typo in one would have silently broken every permission check
that crossed it. Introduced by P4, so this one is self-inflicted.

The naive fix does not work: `PermissionService` already imports `AppRoleService`
(lazily, inside `_app_role_from_registry`), so a module-level import back would
close a cycle. Extracted to `back/objects/registry/roles.py`, a leaf module that
imports nothing, carrying the full vocabulary plus `ROLE_HIERARCHY`,
`ASSIGNABLE_ROLES` and `APP_LEVEL_ROLES`. Both modules re-export, so every existing
`from ...PermissionService import ROLE_ADMIN` still resolves — verified, along with
clean import in both orders.

## 6. `_SAFE_SCHEMA_RE` — checked, and left duplicated on purpose

Identical `re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")` in
`back/core/graphdb/postgres/PostgresBase.py` and
`back/objects/registry/store/postgres/store.py`, both guarding an identifier
before it is interpolated into DDL — so drift is security-relevant, not cosmetic.

It still should not be merged as-is:

- The existing helpers (`SQLHelpers.validate_uc_identifier`,
  `uc/identifiers.UC_IDENTIFIER_RE`) validate **Unity Catalog** identifiers, which
  permit hyphens (`[a-zA-Z0-9_-]`). Postgres bare identifiers do not. They are
  different rules and reusing one for the other would be a bug.
- Importing `PostgresBase` from the registry store would couple the registry to the
  **graph engine** — two unrelated subsystems that merely both need identifier
  validation. That coupling is worse than the duplication.

The correct home is a small module under `back/core/postgres/` (which already owns
Postgres connection identity), used by both. That is a real change with a design
decision in it, so for now both copies carry a cross-reference comment naming the
other and the reason, making drift visible. **Follow-up, not silently dropped.**

## Correction to the audit's own numbers

§B originally reported "5 constants with the same value in more than one module".
That count came from a **hand-picked list of 12 names**, not a full scan. An
unrestricted scan finds more — `ROLE_*`, `_SAFE_SCHEMA_RE`, `_MAX_DEPTH`,
`_PITFALL_RULES_PATH`. Items 5 and 6 above came from re-running it properly after
the first fixes. The remaining two (`_MAX_DEPTH`, `_PITFALL_RULES_PATH`, both 2
sites in `src/agents/`) are low-consequence and untouched.

## Verification

- `uv run --frozen pytest -q -m "not scenario"` → **4930 passed, 299 skipped**,
  unchanged through every chunk.
- `pytest --collect-only` → 5234, `configfile: pyproject.toml`, no warning.
- ruff: F-rule findings **67, identical to HEAD**; total 4705 → 4702.
- Every moved constant proven byte-identical via every surviving import path.
- Per-stage knobs proven still distinct, so no agent was silently retuned.
- black clean on both new modules.

## Untouched, as planned

**B1** (Form Template Method on the four PGE ReAct loops — days, high risk) and
**D** (12 files over the 800-LOC ceiling — weeks). Both remain as written above.
