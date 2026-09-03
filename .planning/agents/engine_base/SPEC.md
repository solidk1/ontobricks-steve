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
Postgres, but its agents still only speak to a Databricks workspace. This change makes the
transport provider-agnostic — any OpenAI-compatible `/chat/completions` endpoint (OpenAI,
Azure OpenAI, vLLM, Ollama, LiteLLM, Bedrock gateways) — and keeps **Databricks Foundation
Model API as a preset**, not a requirement.

Databricks serving endpoints already speak the OpenAI chat-completions request and response
shape. Only two things differ: the URL and whether `model` travels in the body.

## 2. Identity

| Field | Value |
|---|---|
| `agent_name` | *(none — shared infrastructure for all 11 engines)* |
| `module_path` | `src/agents/engine_base.py`, `src/shared/config/LLMTarget.py` |
| `model_endpoint` | Provider-agnostic. Databricks preset resolves `{host}/serving-endpoints/{model}/invocations`; external resolves `{ONTOBRICKS_LLM_BASE_URL}/chat/completions` |
| `temperature` | Unchanged (`0.1` production default; `_UNSUPPORTED_PARAMS` still strips it for models that reject it) |
| `mlflow_experiment` | Unchanged — `ONTOBRICKS_MLFLOW_EXPERIMENT`, default `ontobricks-agents` |

### Affected callers

| Site | Change |
|---|---|
| `agents/engine_base.call_serving_endpoint` | URL/headers/body from `LLMTarget`; signature unchanged |
| `back/core/sqlwizard/SQLWizardService.call_llm_endpoint` | Same URL was hardcoded a second time — now shares the resolver |
| `back/core/helpers/DatabricksHelpers.require_serving_llm` | Stops demanding Databricks credentials when an external LLM is configured |
| `api/routers/internal/dtwin._auto_discover_llm_endpoint` | Offers the configured external model(s) instead of an empty picker |

The 11 engine `run_agent(host, token, endpoint_name, …)` signatures are **deliberately
unchanged** — see §10.

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
| `payload_shape` | `model` key present iff style is `openai` | `1.00` | `0.20` | `contract` (rule-based) |
| `no_secret_leak` | API key absent from all log records | `1.00` | `0.15` | `contract` (rule-based) |
| `live_smoke` | one real round-trip returns non-empty content per configured provider | `1.00` | `0.10` | wall-clock + response parse |

**Aggregate threshold:** weighted sum ≥ `0.90` to pass G2. The four offline dimensions sum
to `0.90`, so the gate is passable without a live endpoint — deliberately, because CI has
no LLM. `live_smoke` is the tenth that only a real provider can supply, and it is recorded
as *not run* rather than assumed.

## 6. Failure modes

| Symptom | Detection | Mitigation |
|---|---|---|
| Silent fallback: external configured but Databricks used (or vice versa) | `url_exact` — every dataset row asserts the full URL, so a mis-resolved style fails loudly | `LLMTarget.resolve` has one precedence rule (explicit external wins) and logs which style it chose, without the key |
| `model` omitted on the OpenAI path | `payload_shape` | `payload_extras()` is derived from `api_style`, not written per-caller |
| API key leaks into logs | `no_secret_leak` asserts on `caplog` records | Never log `api_key`; log `base_url` + `model` + style only |
| Double `/v1/chat/completions` from a base URL that already ends in the path | `url_exact` rows cover `…/v1`, `…/v1/`, `…/v1/chat/completions` | `_normalise_base` strips a trailing `/chat/completions` |
| Empty model picker on an external deployment, so no agent can start | Route test asserts the picker is non-empty | `ONTOBRICKS_LLM_MODELS` (optional CSV) feeds the picker; falls back to `_MODEL` |
| Keyless local provider (Ollama/vLLM) rejected for want of a token | Dataset row with `api_key=""` | `headers()` omits `Authorization` entirely when no key is set |

## 7. Eval dataset

- **Baseline:** `tests/eval/datasets/engine_base/baseline.jsonl` — 24 transport-contract
  cases. Each row is a `(env, host, token, endpoint_name)` input and the exact expected
  URL, `Authorization` header, and `model`-key presence.
- **Regression:** `tests/eval/datasets/engine_base/regression.jsonl` — 3 rows for the
  mistakes this work actually made or nearly made (see §10).
- Mirror at `.planning/agents/engine_base/eval/dataset.jsonl`.

Rows are hand-curated, not synthetic: the interesting cases are exactly the URL-shape edge
cases, and a generator would produce plausible-looking rows that miss them.

## 8. MLflow tracing

Unchanged and still mandatory: `call_serving_endpoint` keeps its `@trace_llm("agent:llm")`
decorator, so both the Databricks and external paths are traced identically. The span name
still comes from each caller's `trace_name`. `LLMTarget` itself is pure config resolution
with no I/O, so it is not traced.

## 9. Plan reference

`.planning/agents/engine_base/PLAN.md`.

## 10. Decisions and their costs

**Engine signatures stay `(host, token, endpoint_name)`.** The honest names would be
`(base_url, api_key, model)`, and `call_serving_endpoint` is now a misnomer. Renaming means
11 engine files plus 12 test modules that patch the symbol by name — ~40 lines of pure
churn mixed into a behaviour change, which is the shape of diff that hides a real bug from
a reviewer. The three positional values already mean "endpoint base, credential, model
name" in both worlds; only the URL shape differed. **Follow-up, not this change:** rename
to `call_chat_completion(target, messages, …)` as a mechanical commit of its own.

**Precedence: explicit external configuration wins.** `LLMTarget.resolve` does not sniff
the hostname to guess the provider — `*.databricks.com` sniffing breaks on custom DNS and
private workspaces. Provenance decides: if `ONTOBRICKS_LLM_BASE_URL` is set, the style is
`openai`; otherwise the caller's triple is Databricks. No third knob.

**`_UNSUPPORTED_PARAMS` is now keyed by resolved model, not `endpoint_name`.** Parameter
support is a property of the model, and on an external provider `endpoint_name` may be a
stale Databricks name that is no longer what gets called.

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
