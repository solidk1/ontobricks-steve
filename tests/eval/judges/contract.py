"""Rule-based judge for the ``engine_base`` LLM transport contract.

Scores the four offline dimensions declared in
``.planning/agents/engine_base/SPEC.md`` §5.  A transport change's meaningful
property is behavioural equivalence, not answer quality, so the judge compares
the resolved URL / auth header / payload shape against an exact expectation
instead of asking a model what it thinks.

Shared deliberately between the pytest suite
(``tests/units/agents/test_engine_base_transport_contract.py``) and the eval
runner (``tests/eval/run_engine_base.py``) so the gate and the test cannot
disagree about what passing means.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from typing import Any
from unittest import mock

# Every ONTOBRICKS_LLM_* key a row may set.  Keys absent from a row's ``env``
# are cleared, so one row can never inherit another's provider.
ENV_KEYS = (
    "ONTOBRICKS_LLM_BASE_URL",
    "ONTOBRICKS_LLM_API_KEY",
    "ONTOBRICKS_LLM_MODEL",
    "ONTOBRICKS_LLM_MODELS",
)

# SPEC §5.  The four offline dimensions sum to 0.90; ``live_smoke`` is the
# remaining 0.10 and is scored only by the runner's --live mode.
DIMENSION_WEIGHTS = {
    "url_exact": 0.35,
    "auth_header_exact": 0.20,
    "payload_shape": 0.20,
    "no_secret_leak": 0.15,
}
LIVE_WEIGHT = 0.10
AGGREGATE_THRESHOLD = 0.90


class _Capture(logging.Handler):
    """Collects formatted records so ``no_secret_leak`` can inspect them."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append(record.getMessage())
        except Exception:  # pragma: no cover - a broken format string
            self.messages.append(repr(record.msg))


def observe(case: dict[str, Any]) -> dict[str, Any]:
    """Resolve one row's ``input`` into the observed transport contract.

    Returns either ``{"error": "<ExceptionClassName>"}`` or the four observable
    facts: resolved URL, ``Authorization`` header (``None`` when omitted), the
    ``model`` value the payload carries (``None`` when absent), and the style.
    """
    from shared.config.LLMTarget import LLMTarget

    env = {k: v for k, v in (case.get("env") or {}).items()}
    patched = {k: env[k] for k in ENV_KEYS if k in env}
    removed = [k for k in ENV_KEYS if k not in env]

    capture = _Capture()
    root = logging.getLogger()
    root.addHandler(capture)
    previous_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        with mock.patch.dict(os.environ, patched, clear=False):
            for key in removed:
                os.environ.pop(key, None)
            try:
                target = LLMTarget.resolve(
                    case.get("host", ""),
                    case.get("token", ""),
                    case.get("endpoint_name", ""),
                )
            except Exception as exc:
                return {"error": type(exc).__name__, "log": list(capture.messages)}
            observed = {
                "url": target.completions_url(),
                "auth": target.headers().get("Authorization"),
                "model_in_body": target.payload_extras().get("model"),
                "api_style": target.api_style,
                "log": list(capture.messages),
            }
    finally:
        root.removeHandler(capture)
        root.setLevel(previous_level)
    return observed


def _secrets_of(case: dict[str, Any]) -> list[str]:
    """Credential strings that must never reach a log record."""
    env = case.get("env") or {}
    candidates = [env.get("ONTOBRICKS_LLM_API_KEY", ""), case.get("token", "")]
    return [c for c in candidates if c and len(c) >= 4]


def judge_row(row: dict[str, Any]) -> dict[str, Any]:
    """Score one dataset row, returning per-dimension booleans."""
    case = row["input"]
    expected = row["expected"]
    observed = observe(case)
    log_blob = "\n".join(observed.get("log", []))

    if "error" in expected:
        # An expected failure exercises only the leak dimension meaningfully;
        # the other three have no URL to compare, so they pass vacuously only
        # when the error itself matched.
        matched = observed.get("error") == expected["error"]
        return {
            "id": row["id"],
            "observed": observed,
            "dimensions": {
                "url_exact": matched,
                "auth_header_exact": matched,
                "payload_shape": matched,
                "no_secret_leak": all(s not in log_blob for s in _secrets_of(case)),
            },
        }

    return {
        "id": row["id"],
        "observed": observed,
        "dimensions": {
            "url_exact": observed.get("url") == expected["url"],
            "auth_header_exact": observed.get("auth") == expected["auth"],
            "payload_shape": (
                observed.get("model_in_body") == expected["model_in_body"]
                and observed.get("api_style") == expected["api_style"]
            ),
            "no_secret_leak": all(s not in log_blob for s in _secrets_of(case)),
        },
    }


def aggregate(results: Iterable[dict[str, Any]], *, live_passed: bool = False) -> float:
    """Weighted aggregate across rows, per SPEC §5.

    ``live_smoke`` contributes ``LIVE_WEIGHT`` only when a real round-trip
    actually happened; it is scored zero — not skipped — otherwise, so the
    reported number never implies a check that did not run.
    """
    rows = list(results)
    if not rows:
        return 0.0
    score = 0.0
    for name, weight in DIMENSION_WEIGHTS.items():
        passed = sum(1 for r in rows if r["dimensions"][name])
        score += weight * (passed / len(rows))
    if live_passed:
        score += LIVE_WEIGHT
    return round(score, 4)
