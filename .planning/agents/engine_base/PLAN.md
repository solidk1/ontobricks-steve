# PLAN: provider-agnostic LLM transport (P6)

Implements `.planning/agents/engine_base/SPEC.md`. Classified **bounded** under
`superpowers:brainstorming` — the flow exists and is being changed, not invented — with
the design captured in the SPEC rather than a separate design doc, because the ai-feature
gate requires a SPEC for this path anyway and two documents would drift.

## Brainstorm decisions

| Question | Decision | Why |
|---|---|---|
| Which layer owns provider choice? | A new `shared/config/LLMTarget.py` | Pure config resolution, no I/O. Sits beside `RuntimeEnv` and `TraversalLimits`, the two other env-derived config classes from this workstream. Keeps `engine_base` responsible only for posting. |
| How is the provider detected? | Provenance: `ONTOBRICKS_LLM_BASE_URL` set ⇒ OpenAI style; otherwise the caller's triple is Databricks | Hostname sniffing (`*.databricks.com`) breaks on custom DNS, private workspaces and proxies, and picks the wrong credential silently. An explicit third `ONTOBRICKS_LLM_API_STYLE` knob is one more thing to get wrong when the answer is already knowable. |
| Change the 11 `run_agent` signatures? | No | See SPEC §10. ~40 lines of rename churn mixed into a behaviour change hides real bugs from reviewers. Deliberate, documented, and left as a mechanical follow-up. |
| What does the eval measure? | Transport contract, not answer quality | A judge scoring answers cannot distinguish a broken transport from a bad answer: a 500 and a hallucination both score zero. |

## Code smells addressed (Fowler)

| Smell | Refactoring | Location |
|---|---|---|
| **Duplicated Code** — the Databricks invocations URL was built in two unrelated files | **Extract Class** `LLMTarget` | `agents/engine_base.py:84`, `back/core/sqlwizard/SQLWizardService.py:286` |
| **Duplicated Code** — three routes carried the same 12-line "require credentials, else auto-discover, else fail" block | **Extract Function** `_require_llm` | `api/routers/internal/dtwin.py` at 700, 2204, 2356 |
| **Primitive Obsession** — `(host, token, endpoint_name)` is a data clump whose meaning depends on an unstated provider | **Introduce Parameter Object** (`LLMTarget`), internally only | `agents/engine_base.py` |

## Steps

1. **SPEC + dataset first.** 24 baseline + 3 regression contract cases, written before any
   source change, and run red (109 failures) to prove they bind to real behaviour.
2. **`shared/config/LLMTarget.py`** — `completions_url`, `headers`, `payload_extras`,
   `describe`, `from_env`, `for_databricks`, `resolve`, `external_configured`,
   `picker_models`.
3. **`agents/engine_base.py`** — resolve a target; URL, headers and the `model` body field
   come from it. Signature untouched. `_UNSUPPORTED_PARAMS` re-keyed from `endpoint_name`
   to the resolved model.
4. **`back/core/sqlwizard/SQLWizardService.py`** — `call_llm_endpoint` shares the resolver;
   `get_model_serving_endpoints` returns configured models when there is no workspace.
5. **`back/core/helpers/DatabricksHelpers.require_serving_llm`** — delegates the decision to
   `LLMTarget.resolve`, so a route cannot admit a request the transport would refuse.
6. **`api/routers/internal/dtwin.py`** — extract `_require_llm`; three call sites collapse
   to one line each.
7. **`api/routers/internal/mapping.py`** — the picker route no longer demands a workspace
   client when an external provider is configured.
8. **Gate** — widen `eval-gate.yml`'s detector to see shared agent infrastructure (SPEC §11).
9. **Docs** — `.env.example`, README, deployment guide.

## Verification

- `tests/units/agents/test_engine_base_transport_contract.py` — 112 tests, dataset-driven.
- `tests/units/agents/test_agent_engine_base.py` — **unchanged and passing**, which is the
  evidence that the Databricks preset is byte-identical.
- `tests/units/core/test_sql_wizard.py` — unchanged and passing.
- `tests/eval/run_engine_base.py` — offline aggregate `0.9000` against a `0.90` threshold.
- Negative control: re-injecting a key into a log line makes `no_secret_leak` fail on 13
  rows; reverting makes it pass. A guard never seen to fail is not a guard.

## Not done

`live_smoke` (SPEC §5, weight `0.10`). Every Databricks CLI profile on this machine has an
expired refresh token and no external provider key is configured, so no real round-trip was
made. Scored **zero**, not skipped, so the reported `0.9000` cannot be mistaken for a
verified end-to-end call. To complete it:

```sh
databricks auth login -p <profile>
uv run --frozen python tests/eval/run_engine_base.py --live \
    --host "$(databricks auth env -p <profile> | jq -r .DATABRICKS_HOST)" \
    --endpoint databricks-claude-sonnet-4
```
