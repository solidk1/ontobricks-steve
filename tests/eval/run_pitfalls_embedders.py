"""Eval: does swapping the embedder change which pitfalls are reported?

The four semantic checks (P4.2, P4.4, P4.5 and the P10 candidate pass) compare
label/comment meaning by cosine similarity against **absolute** thresholds —
``run_p4_2(threshold=0.8)`` and friends. Those constants were tuned against
``all-MiniLM-L6-v2``. A different embedding model has a different cosine
distribution, so the algorithm can be byte-identical and still flag a different
set of pairs.

That is the whole risk of moving embeddings to a service, and it is not something
a unit test can answer: it needs two real models over the same ontology.

    # offline: geometry-only dimensions, no model needed
    uv run --frozen python tests/eval/run_pitfalls_embedders.py

    # the comparison that matters, against a real endpoint
    ONTOBRICKS_LLM_BASE_URL=https://<workspace>/serving-endpoints \
    ONTOBRICKS_LLM_API_KEY=<pat> ONTOBRICKS_EMBEDDING_MODEL=databricks-bge-large-en \
        uv run --frozen python tests/eval/run_pitfalls_embedders.py --live

    # and, where the pitfalls extra is installed, the reference side too
    … --live --compare-local

What is scored, and why these dimensions:

``agreement``      Jaccard overlap of the flagged pair sets, endpoint vs local.
                   The headline number: 1.0 means the swap is behaviourally
                   invisible at the current thresholds.
``rank_agreement`` Spearman-style rank correlation of the similarity scores over
                   the *same* pairs. Separates "the model ranks pairs the same
                   but the scale shifted" (retune the threshold) from "the model
                   disagrees about meaning" (do not swap). This distinction is
                   the actionable part.
``synonym_margin`` On a fixed probe set of known synonym / unrelated label pairs,
                   the gap between the two groups' mean similarity. A model that
                   cannot separate them is unusable here whatever the threshold.
``geometry``       Self-similarity is 1, orthogonality is 0, an empty comment
                   yields 0 rather than nan, and the matrix is symmetric and
                   in-bounds. Offline; guards the numpy cosine that replaced
                   scikit-learn.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".." / "src"))

import numpy as np  # noqa: E402

WEIGHTS = {
    "geometry": 0.20,
    "synonym_margin": 0.25,
    "labelled_accuracy": 0.35,
    "rank_agreement": 0.20,
}

# ``agreement`` (overlap with the incumbent model) was a *scored* dimension in the
# first version of this harness, and it produced a materially wrong verdict:
# gte-large-en flagged all six known-synonym pairs while all-MiniLM-L6-v2 flagged
# two, giving a Jaccard of 0.33 and the headline "DO NOT SWAP: models disagree
# about meaning". The endpoint was a strict superset and correct on every labelled
# pair; the incumbent was simply less sensitive.
#
# Scoring against the incumbent treats it as ground truth. It is not — it is the
# thing being replaced. The probe pairs carry known labels, so accuracy is
# measured against those, and incumbent overlap is reported as context.

#: Pairs whose verdict a usable embedding model must get right, independent of
#: any threshold. Drawn from the vocabulary these checks actually see: class and
#: property labels from generated ontologies.
SYNONYM_PAIRS: List[Tuple[str, str]] = [
    ("Vehicle", "Automobile"),
    ("Person", "Individual"),
    ("Purchase", "Acquisition"),
    ("hasAuthor", "writtenBy"),
    ("Physician", "Doctor"),
    ("Invoice", "Bill"),
]
UNRELATED_PAIRS: List[Tuple[str, str]] = [
    ("Vehicle", "Invoice"),
    ("Person", "Temperature"),
    ("hasAuthor", "hasColour"),
    ("Purchase", "Molecule"),
    ("Physician", "Bridge"),
    ("Invoice", "Galaxy"),
]


def _score_geometry() -> Tuple[float, Dict[str, bool]]:
    from back.core.external.pitfalls.embedders import cosine_similarity

    a = np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
    m = cosine_similarity(a)
    checks = {
        "self_similarity_is_one": bool(np.allclose(np.diag(m), 1.0)),
        "orthogonal_is_zero": bool(
            np.isclose(cosine_similarity(a[0], a[1])[0][0], 0.0)
        ),
        "opposite_is_minus_one": bool(
            np.isclose(cosine_similarity(np.array([1.0, 0.0]), np.array([-1.0, 0.0]))[0][0], -1.0)
        ),
        "empty_comment_is_zero_not_nan": bool(
            np.isclose(cosine_similarity(np.zeros(2), a[0])[0][0], 0.0)
        ),
        "symmetric": bool(np.allclose(m, m.T)),
        "in_bounds": bool(m.min() >= -1.0 and m.max() <= 1.0),
    }
    return sum(checks.values()) / len(checks), checks


def _pair_scores(embedder, pairs: Sequence[Tuple[str, str]]) -> List[float]:
    from back.core.external.pitfalls.embedders import cosine_similarity

    texts = sorted({t for pair in pairs for t in pair})
    vectors = embedder.encode(texts)
    index = {t: i for i, t in enumerate(texts)}
    return [
        float(cosine_similarity(vectors[index[a]], vectors[index[b]])[0][0])
        for a, b in pairs
    ]


def _score_synonym_margin(embedder) -> Tuple[float, Dict[str, float]]:
    syn = _pair_scores(embedder, SYNONYM_PAIRS)
    unrel = _pair_scores(embedder, UNRELATED_PAIRS)
    margin = float(np.mean(syn) - np.mean(unrel))
    # A margin of 0.30 is a comfortable separation for sentence embeddings; less
    # than 0.10 means the model cannot tell these apart at all.
    return float(np.clip(margin / 0.30, 0.0, 1.0)), {
        "synonym_mean": round(float(np.mean(syn)), 4),
        "unrelated_mean": round(float(np.mean(unrel)), 4),
        "margin": round(margin, 4),
        "min_synonym": round(float(np.min(syn)), 4),
        "max_unrelated": round(float(np.max(unrel)), 4),
    }


def _flagged(scores: Sequence[float], pairs: Sequence[Tuple[str, str]], threshold: float):
    return {pairs[i] for i, s in enumerate(scores) if s >= threshold}


def _spearman(a: Sequence[float], b: Sequence[float]) -> float:
    """Rank correlation without scipy (which this change removed the need for)."""
    ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
    if len(ra) < 2:
        return 1.0
    return float(np.corrcoef(ra, rb)[0][1])


def _labelled_accuracy(flagged) -> Dict[str, object]:
    """Precision/recall/F1 against the known labels on the probe set."""
    syn, unrel = set(SYNONYM_PAIRS), set(UNRELATED_PAIRS)
    tp = len(flagged & syn)
    fp = len(flagged & unrel)
    fn = len(syn - flagged)
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "true_positives": tp,
        "false_positives": fp,
        "missed_synonyms": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def _verdict(endpoint: Dict[str, object], local: Dict[str, object], rank: float) -> str:
    """Direction matters: more sensitive with no false positives is better."""
    e_f1, l_f1 = float(endpoint["f1"]), float(local["f1"])
    if endpoint["false_positives"]:
        return "CAUTION: the endpoint flags unrelated pairs; raise the threshold"
    if e_f1 > l_f1:
        return (
            f"IMPROVEMENT: endpoint F1 {e_f1:.2f} vs incumbent {l_f1:.2f} at the "
            "same threshold, with no false positives — thresholds transfer and "
            "recall improves"
        )
    if e_f1 == l_f1:
        return "EQUIVALENT: thresholds transfer as-is"
    if rank > 0.9:
        return "RETUNE: same ranking, shifted scale — lower the threshold"
    return "DO NOT SWAP: the endpoint misses synonyms the incumbent catches"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="call the configured endpoint")
    ap.add_argument("--compare-local", action="store_true", help="also run sentence-transformers")
    ap.add_argument("--threshold", type=float, default=0.8, help="run_p4_2's default")
    args = ap.parse_args()

    results: Dict[str, object] = {}
    scores: Dict[str, float] = {}

    geo, geo_detail = _score_geometry()
    scores["geometry"] = geo
    results["geometry"] = geo_detail

    endpoint = local = None
    if args.live:
        from back.core.external.pitfalls.embedders import EndpointEmbedder

        endpoint = EndpointEmbedder()
        s, detail = _score_synonym_margin(endpoint)
        scores["synonym_margin"] = s
        results["synonym_margin"] = {"embedder": endpoint.name, **detail}

    if args.compare_local:
        from back.core.external.pitfalls.embedders import LocalEmbedder

        local = LocalEmbedder()

    if endpoint is not None:
        all_pairs = SYNONYM_PAIRS + UNRELATED_PAIRS
        e_scores = _pair_scores(endpoint, all_pairs)
        e_flag = _flagged(e_scores, all_pairs, args.threshold)
        e_acc = _labelled_accuracy(e_flag)
        scores["labelled_accuracy"] = e_acc["f1"]
        results["labelled_accuracy"] = {"embedder": endpoint.name, **e_acc}

        if local is not None:
            l_scores = _pair_scores(local, all_pairs)
            l_flag = _flagged(l_scores, all_pairs, args.threshold)
            l_acc = _labelled_accuracy(l_flag)
            scores["rank_agreement"] = max(0.0, _spearman(e_scores, l_scores))
            results["incumbent_comparison"] = {
                "threshold": args.threshold,
                "note": (
                    "Context, not a score. The incumbent is what is being "
                    "replaced, not ground truth."
                ),
                "endpoint": {"flagged": sorted("|".join(p) for p in e_flag), **e_acc},
                "local": {"flagged": sorted("|".join(p) for p in l_flag), **l_acc},
                "only_endpoint": sorted("|".join(p) for p in (e_flag - l_flag)),
                "only_local": sorted("|".join(p) for p in (l_flag - e_flag)),
                "verdict": _verdict(e_acc, l_acc, scores.get("rank_agreement", 0.0)),
            }

    aggregate = sum(scores[k] * WEIGHTS[k] for k in scores) / sum(
        WEIGHTS[k] for k in scores
    )
    payload = {
        "dimensions": {k: round(v, 4) for k, v in scores.items()},
        "aggregate": round(aggregate, 4),
        "scored": sorted(scores),
        "not_scored": sorted(set(WEIGHTS) - set(scores)),
        "detail": results,
    }
    print(json.dumps(payload, indent=2, default=str))

    if os.getenv("MLFLOW_TRACKING_URI"):
        try:
            import mlflow

            with mlflow.start_run(run_name="pitfalls_embedders"):
                for k, v in scores.items():
                    mlflow.log_metric(k, v)
                mlflow.log_metric("aggregate", aggregate)
                mlflow.log_dict(payload, "pitfalls_embedders.json")
        except Exception as exc:  # noqa: BLE001
            print(f"(MLflow logging skipped: {exc})", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
