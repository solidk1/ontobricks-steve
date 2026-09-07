"""Eval harness for the shared LLM transport (``src/agents/engine_base.py``).

Unlike the per-agent harnesses, the subject here is a *transport*, so the
property under test is behavioural equivalence rather than answer quality: with
the same model behind it, a request must come out the same shape it did before.
A judge scoring answer quality cannot tell a broken transport from a bad answer
— a 500 and a hallucination both score zero. The dimensions are therefore
contract-shaped and, deliberately, checkable with no LLM at all:

    uv run --frozen python tests/eval/run_engine_base.py

That offline run scores 0.90 of the 1.00 declared in
``.planning/agents/engine_base/SPEC.md`` §5 — exactly the aggregate threshold,
because CI has no model to call. The remaining 0.10 is ``live_smoke``, one real
round-trip, which needs a reachable provider:

    # Any OpenAI-compatible provider, Databricks included
    ONTOBRICKS_LLM_BASE_URL=https://api.openai.com/v1 \
    ONTOBRICKS_LLM_API_KEY=sk-… ONTOBRICKS_LLM_MODEL=gpt-4o-mini \
        uv run --frozen python tests/eval/run_engine_base.py --live

    ONTOBRICKS_LLM_BASE_URL=https://<workspace>/serving-endpoints \
    ONTOBRICKS_LLM_API_KEY=<pat> ONTOBRICKS_LLM_MODEL=databricks-claude-sonnet-4 \
        uv run --frozen python tests/eval/run_engine_base.py --live

``live_smoke`` is scored zero when it does not run — never skipped — so the
reported aggregate can never imply a check that never happened.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "src", _ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from tests.eval.judges import contract  # noqa: E402

DATASET_DIR = _ROOT / "tests/eval/datasets/engine_base"
THRESHOLDS_PATH = _ROOT / "tests/eval/thresholds.yaml"
SUBJECT = "engine_base"


def _load(name: str) -> list[dict[str, Any]]:
    path = DATASET_DIR / name
    with path.open() as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _threshold() -> float:
    """Read the aggregate threshold, falling back to the SPEC's declared value."""
    try:
        import yaml

        with THRESHOLDS_PATH.open() as fh:
            data = yaml.safe_load(fh) or {}
        return float(
            data.get(SUBJECT, {}).get("aggregate", contract.AGGREGATE_THRESHOLD)
        )
    except Exception as exc:
        print(f"[warn] could not read {THRESHOLDS_PATH}: {exc}")
        return contract.AGGREGATE_THRESHOLD


def live_smoke(model: str = "", *, timeout: int = 60) -> dict[str, Any]:
    """One real round-trip through the production transport.

    Deliberately calls ``call_chat_completion`` — not a reimplementation — so the
    smoke test exercises the same code an agent does, including retry and tracing.
    """
    from agents.engine_base import call_chat_completion, extract_message_content
    from shared.config.LLMTarget import LLMTarget

    target = LLMTarget.from_env(model)
    print(f"  live target: {target.describe()}")
    t0 = time.time()
    response = call_chat_completion(
        target,
        [{"role": "user", "content": "Reply with the single word: OK"}],
        max_tokens=16,
        timeout=timeout,
        trace_name="eval:engine_base:live_smoke",
    )
    elapsed_ms = int((time.time() - t0) * 1000)
    content = extract_message_content(response)
    return {
        "passed": bool(content.strip()),
        "latency_ms": elapsed_ms,
        "content": content.strip()[:120],
        "model": target.model,
        "base_url": target.base_url,
    }


def run(
    *,
    live: bool = False,
    model: str = "",
    mlflow_experiment: str | None = None,
) -> float:
    rows = _load("baseline.jsonl") + _load("regression.jsonl")
    results = [contract.judge_row(r) for r in rows]

    print(f"engine_base transport contract — {len(rows)} cases")
    per_dimension: dict[str, float] = {}
    for name in contract.DIMENSION_WEIGHTS:
        passed = sum(1 for r in results if r["dimensions"][name])
        per_dimension[name] = passed / len(results)
        mark = "ok  " if passed == len(results) else "FAIL"
        print(f"  [{mark}] {name:<20} {passed}/{len(results)}")

    failures = [r for r in results if not all(r["dimensions"].values())]
    for r in failures:
        bad = [k for k, v in r["dimensions"].items() if not v]
        print(f"    - {r['id']}: {', '.join(bad)}  observed={r['observed']}")

    smoke: dict[str, Any] = {"passed": False, "reason": "not run"}
    if live:
        try:
            smoke = live_smoke(model)
            mark = "ok  " if smoke["passed"] else "FAIL"
            print(
                f"  [{mark}] live_smoke           {smoke['latency_ms']}ms "
                f"-> {smoke['content']!r}"
            )
        except Exception as exc:
            smoke = {"passed": False, "reason": f"{type(exc).__name__}: {exc}"}
            print(f"  [FAIL] live_smoke           {smoke['reason']}")
    else:
        print("  [----] live_smoke           not run (offline); scored 0.0")

    aggregate = contract.aggregate(results, live_passed=bool(smoke.get("passed")))
    threshold = _threshold()
    print(f"\naggregate {aggregate:.4f} (threshold {threshold})")

    try:
        import mlflow

        mlflow.set_experiment(mlflow_experiment or "ontobricks-agents")
        with mlflow.start_run(run_name=f"{SUBJECT}-{'live' if live else 'offline'}"):
            mlflow.log_metric("aggregate_score", aggregate)
            for name, value in per_dimension.items():
                mlflow.log_metric(f"avg_{name}", value)
            mlflow.log_metric("live_smoke", 1.0 if smoke.get("passed") else 0.0)
            mlflow.log_param("live", live)
            mlflow.log_param("cases", len(rows))
            if not smoke.get("passed"):
                mlflow.set_tag("live_smoke_reason", str(smoke.get("reason", "")))
            for name in ("baseline.jsonl", "regression.jsonl"):
                mlflow.log_artifact(str(DATASET_DIR / name))
            run_info = mlflow.active_run().info
            print(f"MLflow: experiment={run_info.experiment_id} run={run_info.run_id}")
    except Exception as exc:
        print(f"[warn] MLflow logging skipped: {exc}")

    if aggregate < threshold:
        print(f"FAIL: aggregate {aggregate:.4f} < threshold {threshold}")
    return aggregate


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Eval harness — engine_base transport")
    parser.add_argument("--live", action="store_true", help="Add one real round-trip.")
    parser.add_argument(
        "--model", default="", help="Override ONTOBRICKS_LLM_MODEL for the smoke call."
    )
    parser.add_argument("--mlflow-experiment", default=None)
    args = parser.parse_args()

    score = run(
        live=args.live,
        model=args.model,
        mlflow_experiment=args.mlflow_experiment,
    )
    sys.exit(0 if score >= _threshold() else 1)
