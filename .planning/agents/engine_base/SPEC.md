# SPEC: engine_base (shared LLM transport)

> Subject is **shared agent infrastructure**, not one agent: `src/agents/engine_base.py`
> is the single LLM transport for all 11 agent engines. A change here is a change to
> every agent at once, which is why it gets a SPEC of its own rather than 11 copies.
>
> Required by `.cursor/12-ai-feature-lifecycle.mdc`. See §11 for why this subject needed
> the CI detector widened before the gate could see it at all.

---

## 1. Purpose

Every OntoBricks agent reaches its model through one line:

```python
url = f"{host.rstrip('/')}/serving-endpoints/{endpoint_name}/invocations"
```

That URL shape is Databricks-only. It is the last hard Databricks coupling in the agent
layer: after the Apps and Lakebase work, OntoBricks runs on any container against any
Postgres, but its agents still only speak to a Databricks workspace. This change makes the transport
provider-agnostic: one URL shape, one credential, one model field, for any
OpenAI-compatible `/chat/completions` endpoint.

**Databricks is one such provider, not a special case.** Foundation Model APIs serve the
OpenAI shape at `{workspace}/serving-endpoints` — the documented `base_url` for the OpenAI
SDK — with the serving-endpoint name as the model. So it is configured exactly like OpenAI,
Azure OpenAI, vLLM, Ollama or a LiteLLM proxy.

**Nothing is inferred and nothing falls back.** The first revision of this work kept a
Databricks preset at `{host}/serving-endpoints/{model}/invocations` and chose between it and
an external provider by whichever happened to be configured. That is two URL shapes, two
credential sources, and a provider selected as a side effect of unrelated settings — so a
workspace configured for Unity Catalog reads silently became the model provider, and a
typo'd base URL silently changed which model answered. Absent configuration is now an error
naming the variable to set.

## 2. Identity

| Field | Value |
|---|---|
| `agent_name` | *(none — shared infrastructure for all 11 engines)* |
| `module_path` | `src/agents/engine_base.py`, `src/shared/config/LLMTarget.py` |
| `model_endpoint` | `{ONTOBRICKS_LLM_BASE_URL}/chat/completions`, always. No second shape. |
| `temperature` | Unchanged (`0.1` production default; `_UNSUPPORTED_PARAMS` still strips it for models that reject it) |
| `mlflow_experiment` | Unchanged — `ONTOBRICKS_MLFLOW_EXPERIMENT`, default `ontobricks-agents` |

### Affected callers

| Site | Change |
|---|---|
| `agents/engine_base` | `call_serving_endpoint(host, token, endpoint_name, …)` → `call_chat_completion(target, …)` |
| 11 engines + `AgentClient` + 3 domain objects | `endpoint_name: str` → `target: LLMTarget`; `host`/`token` stay, for the document tools only |
| `back/core/sqlwizard/SQLWizardService` | The same URL was hardcoded a second time; dual path collapsed to one. `get_model_serving_endpoints` no longer calls the workspace API |
| `back/core/helpers/DatabricksHelpers.require_serving_llm` | Returns `(host, token, LLMTarget)`; stops treating Databricks credentials as an LLM precondition |
| `api/routers/internal/dtwin` | `_auto_discover_llm_endpoint` **deleted** (73 lines) — it asked the workspace to guess a model |
| `front/static/mapping/js/mapping-shared.js` | Request field `endpoint_name` → `model` |

**`host`/`token` survive on the engine signatures on purpose.** `agents/tools/documents.py`
uses `ctx.host`/`ctx.token` for the Databricks Files API, so they are the *connector*
credentials, not the LLM's. Conflating the two is what made the old design look natural.

## 3. Tool surface

Unchanged. This change is below the tool layer: `tools` is passed through the payload
untouched, and `dispatch_tool` is not modified. No agent gains or loses a tool.

## 4. Success criteria

1. **Databricks preset is byte-identical to today.** With no new environment variables
   set, `call_serving_endpoint("https://w.databricks.com", "tok", "databricks-claude", …)`
   POSTs to `https://w.databricks.com/serving-endpoints/databricks-claude/invocations`,
   with `Authorization: Bearer tok`, and **no `model` key** in the body.
2. **External provider works with Databricks absent.** With
   `ONTOBRICKS_LLM_BASE_URL=https://api.openai.com/v1`, `ONTOBRICKS_LLM_API_KEY=sk-…`,
   `ONTOBRICKS_LLM_MODEL=gpt-4o-mini` and `DATABRICKS_HOST` unset, the same call POSTs to
   `https://api.openai.com/v1/chat/completions` with `"model": "gpt-4o-mini"` in the body.
3. **The domain LLM picker keeps working as a model picker.** An external deployment with
   no `ONTOBRICKS_LLM_MODEL` uses the domain's saved `llm_endpoint` as the model name, so
   the existing Domain Settings dropdown selects models instead of Databricks endpoints.

## 5. Eval dimensions

This is a **transport** change, so the meaningful property is *behavioural equivalence*,
not answer quality: pointed at the same model, the agent must produce the same output it
does today. A judge scoring answer quality cannot distinguish a working transport from a
broken one — a 500 and a hallucination both score zero. The dimensions are therefore
contract-shaped and checkable offline, with one live dimension that needs an endpoint.

| Dimension | Metric | Threshold | Weight | Judge |
|---|---|---|---|---|
| `url_exact` | exact-match on resolved URL per case | `1.00` | `0.35` | `contract` (rule-based) |
| `auth_header_exact` | exact-match on `Authorization` header | `1.00` | `0.20` | `contract` (rule-based) |
| `payload_shape` | `model` present in every request body | `1.00` | `0.20` | `contract` (rule-based) |
| `no_secret_leak` | API key absent from all log records | `1.00` | `0.15` | `contract` (rule-based) |
| `live_smoke` | one real round-trip returns non-empty content per configured provider | `1.00` | `0.10` | wall-clock + response parse |

**Aggregate threshold:** weighted sum ≥ `0.90` to pass G2. The four offline dimensions sum
to `0.90`, so the gate is passable without a live endpoint — deliberately, because CI has
no LLM. `live_smoke` is the tenth that only a real provider can supply, and it is recorded
as *not run* rather than assumed.

## 6. Failure modes

| Symptom | Detection | Mitigation |
|---|---|---|
| Silent fallback to a workspace that was only configured for Unity Catalog | `reg-no-silent-databricks-fallback` and `databricks-host-alone-does-not-configure-an-llm` both require a `ValidationError` | There is no Databricks code path left to fall back to |
| `model` omitted from the body | `payload_shape` on all 26 rows | `call_chat_completion` writes `"model": target.model` unconditionally |
| API key leaks into logs | `no_secret_leak` asserts on `caplog` records | Never log `api_key`; log `base_url` + `model` + style only |
| Double `/v1/chat/completions` from a base URL that already ends in the path | `url_exact` rows cover `…/v1`, `…/v1/`, `…/v1/chat/completions` | `_normalise_base` strips a trailing `/chat/completions` |
| Silently changed model because a workspace endpoint was auto-discovered | The discovery code is deleted; an unconfigured provider raises | Models are declared in `ONTOBRICKS_LLM_MODELS`, never discovered |
| Frontend still sends a field the route stopped reading | `TestLLMRequestFieldNames` in `tests/units/front/test_frontend_canonical_values.py` | Guard verified by re-injecting the regression |
| Keyless local provider (Ollama/vLLM) rejected for want of a token | Dataset row with `api_key=""` | `headers()` omits `Authorization` entirely when no key is set |

## 7. Eval dataset

- **Baseline:** `tests/eval/datasets/engine_base/baseline.jsonl` — 23 transport-contract
  cases. Each row is an `(env, model)` input and the exact expected URL, `Authorization`
  header and body `model`. Five rows require a `ValidationError`.
- **Regression:** `tests/eval/datasets/engine_base/regression.jsonl` — 3 rows, the first of
  which fails on any reintroduction of the Databricks fallback this revision removed.
- Mirror at `.planning/agents/engine_base/eval/dataset.jsonl`.

Rows are hand-curated, not synthetic: the interesting cases are exactly the URL-shape edge
cases, and a generator would produce plausible-looking rows that miss them.

## 8. MLflow tracing

Unchanged and still mandatory: `call_chat_completion` keeps its `@trace_llm("agent:llm")`
decorator. The span's `endpoint` attribute now comes from `target.describe()`, which is
log-safe — base URL and model, never the key. The span name
still comes from each caller's `trace_name`. `LLMTarget` itself is pure config resolution
with no I/O, so it is not traced.

## 9. Plan reference

`.planning/agents/engine_base/PLAN.md`.

## 10. Decisions and their costs

**The deferred rename is done.** The first revision kept `call_serving_endpoint(host,
token, endpoint_name, …)` to avoid churn and recorded the rename as a follow-up. Removing
the Databricks preset forces it: the function no longer accepts a host or a token, so the
signature had to change and the old name became a lie. `call_chat_completion(target, …)`,
with `endpoint_name: str` → `target: LLMTarget` across 11 engines, `AgentClient`, three
domain objects and 12 test modules.

**No hostname sniffing, and no precedence rule either.** The earlier design chose a
provider by provenance, which was better than sniffing `*.databricks.com` but still a
choice being made on the operator's behalf. There is now nothing to choose between.

**What is *not* a fallback, and stays.** Three behaviours could be mistaken for one:
`ONTOBRICKS_LLM_MODEL` as a declared default that a domain may override (explicit config
layering, not a chain); omitting `Authorization` when no key is set (the correct request for
Ollama and a bare vLLM, which reject a `Bearer` carrying nothing); and stripping a trailing
`/chat/completions` from a pasted base URL (input normalisation — doubling it yields a 404
that blames the provider). Each has dataset rows.

**`_UNSUPPORTED_PARAMS` is keyed by the target's model.** Parameter support is a property of
the model, so a `temperature` ban discovered for one model never silences a parameter a
different model supports.

## 11. Gate coverage gap found while writing this

`.github/workflows/eval-gate.yml` detects changed agents with
`grep -E '^src/agents/agent_[a-z_]+/'`. `src/agents/engine_base.py` does not match, so
`changed_agents` is empty, so **every gate job skips via
`if: needs.detect.outputs.changed_agents != ''`**. The one file shared by all 11 agents —
the highest-leverage file in the agent tree — was invisible to the gate that exists to
protect them.

Rather than quietly benefit from the hole, this change widens the detector: shared-infra
paths (`engine_base.py`, `llm_utils.py`, `tracing.py`, `serialization.py`) map to the
sentinel subject `engine_base`, which has this SPEC and its own dataset. Reported in §12 of
the changelog entry.

## 12. Sign-off

- [x] Author has filled every section.
- [x] Offline dimensions run and recorded (`url_exact`, `auth_header_exact`,
      `payload_shape`, `no_secret_leak`).
- [ ] `live_smoke` — **not run.** Requires a reachable LLM endpoint; every Databricks CLI
      profile on this machine has an expired refresh token, and no external provider key is
      configured. `databricks auth login -p <profile>` then
      `uv run --frozen python tests/eval/run_engine_base.py --live` completes it.
- [ ] MLflow eval run URI in PR body — offline run writes locally when
      `MLFLOW_TRACKING_URI` is unset; a workspace URI needs the login above.
