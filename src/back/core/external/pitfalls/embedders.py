"""Sentence embeddings for the semantic pitfall checks.

Four of the nineteen checks — P4.2 (synonyms in superclasses), P4.4 (subclasses
with the same semantics as their superclass), P4.5 (synonyms in properties) and
the P10 candidate pass — ask *"are these two labels the same idea in different
words?"*. String comparison cannot answer that, so labels and comments are
embedded and compared by cosine similarity.

Upstream ran ``sentence-transformers`` locally, which means PyTorch. That took the
container image from **0.50 GB to 6.02 GB** (measured) for those four checks, and
at that size cold starts and rollouts stalled on ``PullingImage``. The extra was
therefore opt-in, so in practice the semantic checks never ran.

The work is embedding a few hundred short strings — a service call, not a reason
to ship a deep-learning runtime. :class:`EndpointEmbedder` posts to the
OpenAI-compatible ``/embeddings`` path on the provider already configured for
chat, and :class:`LocalEmbedder` keeps the original path for offline use.

Both expose one method, ``encode(texts) -> np.ndarray`` of shape
``(len(texts), dim)``, which is the entire surface ``runner.py`` uses. The checks,
their thresholds and the 0.4/0.6 label/description weighting are untouched.

**A caveat that matters.** The thresholds in ``runner.py`` are absolute constants
(``threshold: float = 0.8``) tuned against ``all-MiniLM-L6-v2``. A different model
has a different cosine distribution, so swapping the embedder can change which
pairs are flagged even though the algorithm is identical.
``tests/eval/run_pitfalls_embedders.py`` measures exactly that, and the result is
not uniform across models — on a labelled probe set of ontology labels:

===============================  ======  ==========================================
model                            margin  verdict at threshold 0.8
===============================  ======  ==========================================
databricks-gte-large-en          0.333   F1 **1.00**, no false positives — use this
databricks-qwen3-embedding-0-6b  0.298   separable, but a thin extremes gap
databricks-bge-large-en          0.166   **overlaps** — no threshold separates it
all-MiniLM-L6-v2 (in-process)    —       F1 0.50; misses 4 of 6 known synonyms
===============================  ======  ==========================================

So the endpoint path is not merely lighter: with ``gte-large-en`` it is *more
accurate than the model it replaces*, at the same threshold, which is why the
constants did not need retuning. With ``bge-large-en`` it would silently report
almost nothing, since its synonym mean (0.77) sits below the 0.8 cut.
"""

from __future__ import annotations

from typing import List, Protocol, Sequence

import numpy as np

from back.core.errors import InfrastructureError
from back.core.logging import get_logger

logger = get_logger(__name__)

#: Texts per request. Providers cap both batch size and payload bytes; short
#: labels and comments mean this is comfortably inside typical limits while
#: still turning a few hundred strings into a handful of round trips.
_BATCH = 96

#: Seconds. Embedding a batch of short strings is fast; a hang here would stall
#: an interactive ontology generation, so fail rather than wait.
_TIMEOUT = 60


class Embedder(Protocol):
    """Anything that can turn texts into vectors.

    ``runner.py`` needs nothing else, which is why swapping the implementation
    requires no change to the checks.
    """

    def encode(self, texts: Sequence[str], **kwargs: object) -> np.ndarray:
        ...


def cosine_similarity(a: np.ndarray, b: np.ndarray | None = None) -> np.ndarray:
    """Row-wise cosine similarity, replacing ``sklearn.metrics.pairwise``.

    Four lines of numpy in place of scikit-learn and scipy, which were in the
    dependency set for this one function. numpy is already present in the base
    environment via pandas.

    Zero vectors are given a norm of 1 so they yield 0 similarity rather than
    ``nan``; an empty comment is a legitimate input here and must not poison a
    whole similarity matrix.
    """
    a = np.asarray(a, dtype=float)
    if a.ndim == 1:
        a = a.reshape(1, -1)
    b = a if b is None else np.asarray(b, dtype=float)
    if b.ndim == 1:
        b = b.reshape(1, -1)

    a_norm = a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-12)
    b_norm = b / np.maximum(np.linalg.norm(b, axis=1, keepdims=True), 1e-12)
    return np.clip(a_norm @ b_norm.T, -1.0, 1.0)


class EndpointEmbedder:
    """Embeddings from an OpenAI-compatible ``/embeddings`` endpoint."""

    def __init__(self, target: object | None = None) -> None:
        from shared.config.LLMTarget import LLMTarget

        self._target = target or LLMTarget.for_embeddings()

    @property
    def name(self) -> str:
        return f"endpoint:{getattr(self._target, 'model', '?')}"

    def encode(self, texts: Sequence[str], **_kwargs: object) -> np.ndarray:
        """Embed *texts*, preserving order.

        Empty strings are embedded as zero vectors rather than sent: a provider
        may reject them, and a zero vector is the correct answer for "no
        description" once :func:`cosine_similarity` maps it to 0 similarity.
        """
        items = list(texts)
        if not items:
            return np.zeros((0, 0), dtype=float)

        wanted = [(i, t) for i, t in enumerate(items) if (t or "").strip()]
        vectors: dict[int, List[float]] = {}
        for start in range(0, len(wanted), _BATCH):
            batch = wanted[start : start + _BATCH]
            for (idx, _), vec in zip(batch, self._embed_batch([t for _, t in batch])):
                vectors[idx] = vec

        dim = len(next(iter(vectors.values()))) if vectors else 1
        out = np.zeros((len(items), dim), dtype=float)
        for idx, vec in vectors.items():
            out[idx] = vec
        return out

    def _embed_batch(self, batch: Sequence[str]) -> List[List[float]]:
        import requests

        from agents.tracing import trace_llm

        @trace_llm(name="pitfalls_embeddings")
        def _post(payload: dict) -> dict:
            resp = requests.post(
                self._target.embeddings_url(),
                headers=self._target.headers(),
                json=payload,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()

        try:
            body = _post({"model": self._target.model, "input": list(batch)})
        except Exception as exc:  # noqa: BLE001 — vendor/network surface
            raise InfrastructureError(
                "Embedding request failed",
                detail=f"{self._target.describe()}: {exc}",
            ) from exc

        data = body.get("data") or []
        if len(data) != len(batch):
            raise InfrastructureError(
                "Embedding response did not match the request",
                detail=f"asked for {len(batch)} vectors, received {len(data)}",
            )
        # Providers are not required to return results in order.
        ordered = sorted(data, key=lambda d: int(d.get("index", 0)))
        return [list(d.get("embedding") or []) for d in ordered]


class LocalEmbedder:
    """The original in-process ``sentence-transformers`` path.

    Kept for air-gapped installs and as the reference the endpoint embedder is
    evaluated against. Requires the ``pitfalls-local`` extra (PyTorch).
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        self.model_name = model_name
        self._model = None

    @property
    def name(self) -> str:
        return f"local:{self.model_name}"

    def encode(self, texts: Sequence[str], **_kwargs: object) -> np.ndarray:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise InfrastructureError(
                    "The local embedder needs the 'pitfalls-local' extra",
                    detail=(
                        "uv sync --extra pitfalls-local, or configure "
                        "ONTOBRICKS_EMBEDDING_MODEL to use an endpoint instead "
                        "(no PyTorch required)."
                    ),
                ) from exc
            self._model = SentenceTransformer(self.model_name)
        return np.asarray(self._model.encode(list(texts), show_progress_bar=False))


def resolve_embedder(model_name: str = "all-MiniLM-L6-v2") -> Embedder:
    """The configured embedder: endpoint when available, else local.

    Endpoint first because it is the path that works in a normal container. The
    local fallback keeps existing installs behaving exactly as before.
    """
    from shared.config.LLMTarget import LLMTarget

    if LLMTarget.embedding_model():
        embedder = EndpointEmbedder()
        logger.info("Pitfalls semantic checks using %s", embedder.name)
        return embedder

    logger.info(
        "ONTOBRICKS_EMBEDDING_MODEL is not set; falling back to the in-process "
        "model %s (requires the pitfalls-local extra)",
        model_name,
    )
    return LocalEmbedder(model_name)
