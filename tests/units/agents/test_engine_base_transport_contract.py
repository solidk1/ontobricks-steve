"""The LLM transport contract, driven by the committed eval dataset.

``src/agents/engine_base.py`` is the single LLM transport for all 11 agent
engines, so a mistake here breaks every agent at once.  The dataset at
``tests/eval/datasets/engine_base/`` is the same one the G2 eval gate reads;
running it as a unit test means a contract regression fails locally in
milliseconds instead of waiting for a nightly eval.

See ``.planning/agents/engine_base/SPEC.md`` for the dimensions and why they
are contract-shaped rather than judge-scored.
"""

import json
import pathlib

import pytest

from tests.eval.judges import contract

pytestmark = pytest.mark.unit

_DATASETS = (
    pathlib.Path(__file__).resolve().parents[2] / "eval" / "datasets" / "engine_base"
)


def _load(name: str):
    path = _DATASETS / name
    with path.open() as fh:
        return [json.loads(line) for line in fh if line.strip()]


BASELINE = _load("baseline.jsonl")
REGRESSION = _load("regression.jsonl")
ALL_ROWS = BASELINE + REGRESSION


def _ids(rows):
    return [r["id"] for r in rows]


class TestTransportContract:
    """Every dataset row, one dimension per test, so a failure names itself."""

    @pytest.mark.parametrize("row", ALL_ROWS, ids=_ids(ALL_ROWS))
    def test_resolved_url_matches_expectation(self, row):
        result = contract.judge_row(row)
        assert result["dimensions"]["url_exact"], (
            f"{row['id']}: expected {row['expected'].get('url') or row['expected'].get('error')!r}, "
            f"observed {result['observed'].get('url') or result['observed'].get('error')!r}"
        )

    @pytest.mark.parametrize("row", ALL_ROWS, ids=_ids(ALL_ROWS))
    def test_authorization_header_matches_expectation(self, row):
        result = contract.judge_row(row)
        assert result["dimensions"]["auth_header_exact"], (
            f"{row['id']}: expected auth {row['expected'].get('auth')!r}, "
            f"observed {result['observed'].get('auth')!r}"
        )

    @pytest.mark.parametrize("row", ALL_ROWS, ids=_ids(ALL_ROWS))
    def test_payload_carries_model_only_for_openai_style(self, row):
        result = contract.judge_row(row)
        assert result["dimensions"]["payload_shape"], (
            f"{row['id']}: expected model={row['expected'].get('model_in_body')!r} "
            f"style={row['expected'].get('api_style')!r}, observed "
            f"model={result['observed'].get('model_in_body')!r} "
            f"style={result['observed'].get('api_style')!r}"
        )

    @pytest.mark.parametrize("row", ALL_ROWS, ids=_ids(ALL_ROWS))
    def test_credentials_never_reach_a_log_record(self, row):
        result = contract.judge_row(row)
        assert result["dimensions"][
            "no_secret_leak"
        ], f"{row['id']}: a credential appeared in a log record"


class TestDatasetItself:
    """The gate reads these files; guard their shape too."""

    def test_baseline_meets_the_gate_minimum(self):
        assert len(BASELINE) >= 10

    def test_every_row_has_a_unique_id(self):
        ids = _ids(ALL_ROWS)
        assert len(ids) == len(set(ids))

    def test_mirror_matches_the_committed_baseline(self):
        mirror = (
            pathlib.Path(__file__).resolve().parents[2].parent
            / ".planning/agents/engine_base/eval/dataset.jsonl"
        )
        assert mirror.read_text() == (_DATASETS / "baseline.jsonl").read_text()

    def test_aggregate_clears_the_declared_threshold_offline(self):
        results = [contract.judge_row(r) for r in ALL_ROWS]
        score = contract.aggregate(results, live_passed=False)
        assert score >= contract.AGGREGATE_THRESHOLD, (
            f"aggregate {score} < {contract.AGGREGATE_THRESHOLD}; failing rows: "
            + ", ".join(r["id"] for r in results if not all(r["dimensions"].values()))
        )
