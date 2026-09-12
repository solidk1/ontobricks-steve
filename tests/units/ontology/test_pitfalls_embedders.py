"""The semantic pitfall checks get embeddings from a service, not PyTorch.

Why this file exists
--------------------
Four of the nineteen checks compare label/comment meaning by cosine similarity,
which upstream did with ``sentence-transformers`` in-process. That dependency
brings PyTorch, taking the image from **0.50 GB to 6.02 GB** (measured) — at
which size cold starts and rollouts stalled on ``PullingImage``, so the extra was
opt-in and the semantic checks never actually ran in a deployment.

Embedding a few hundred short strings is a service call. ``EndpointEmbedder``
posts to the OpenAI-compatible ``/embeddings`` path on the provider already
configured for chat, so the four checks need no extra at all.

The seam is deliberately one method — ``encode(texts) -> ndarray`` — so the
checks, their absolute thresholds and the 0.4/0.6 label/description weighting are
untouched. These tests pin that seam, the ordering and padding rules that make it
a safe substitution, and the numpy cosine that replaced scikit-learn.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from back.core.errors import InfrastructureError, ValidationError
from back.core.external.pitfalls.embedders import (
    EndpointEmbedder,
    cosine_similarity,
    resolve_embedder,
)
from shared.config.LLMTarget import LLMTarget

pytestmark = pytest.mark.unit

_TARGET = LLMTarget(base_url="https://x/v1", api_key="k", model="bge-large-en")


def _response(vectors, *, shuffle=False):
    data = [{"index": i, "embedding": v} for i, v in enumerate(vectors)]
    if shuffle:
        data = list(reversed(data))
    resp = MagicMock()
    resp.json.return_value = {"data": data}
    resp.raise_for_status.return_value = None
    return resp


class TestCosineSimilarity:
    """Four lines of numpy replacing scikit-learn and scipy."""

    def test_identical_vectors_are_one(self):
        a = np.array([[1.0, 2.0, 3.0]])
        assert cosine_similarity(a)[0][0] == pytest.approx(1.0)

    def test_orthogonal_vectors_are_zero(self):
        got = cosine_similarity(np.array([1.0, 0.0]), np.array([0.0, 1.0]))
        assert got[0][0] == pytest.approx(0.0)

    def test_opposite_vectors_are_minus_one(self):
        got = cosine_similarity(np.array([1.0, 0.0]), np.array([-1.0, 0.0]))
        assert got[0][0] == pytest.approx(-1.0)

    def test_zero_vector_gives_zero_not_nan(self):
        """An absent rdfs:comment is a legitimate input and must not poison the
        whole similarity matrix with nan."""
        got = cosine_similarity(np.zeros(3), np.array([1.0, 2.0, 3.0]))
        assert got[0][0] == pytest.approx(0.0)
        assert not np.isnan(got).any()

    def test_pairwise_matrix_is_square_and_symmetric(self):
        m = cosine_similarity(np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]))
        assert m.shape == (3, 3)
        assert np.allclose(m, m.T)

    def test_stays_within_bounds(self):
        m = cosine_similarity(np.random.default_rng(0).normal(size=(12, 8)))
        assert m.min() >= -1.0 and m.max() <= 1.0


class TestEndpointEmbedder:
    def test_returns_one_vector_per_input_in_order(self):
        e = EndpointEmbedder(_TARGET)
        with patch("requests.post", return_value=_response([[1.0, 0.0], [0.0, 1.0]])):
            out = e.encode(["alpha", "beta"])
        assert out.shape == (2, 2)
        assert out[0].tolist() == [1.0, 0.0]

    def test_out_of_order_provider_response_is_reordered(self):
        """The API does not guarantee ordering; ``index`` is authoritative.
        Getting this wrong would silently pair labels with the wrong vectors."""
        e = EndpointEmbedder(_TARGET)
        with patch("requests.post", return_value=_response([[1.0, 0.0], [0.0, 1.0]], shuffle=True)):
            out = e.encode(["alpha", "beta"])
        assert out[0].tolist() == [1.0, 0.0]

    def test_empty_strings_are_not_sent_but_keep_their_slot(self):
        """Positions must line up with the caller's list, and a provider may
        reject an empty input outright."""
        e = EndpointEmbedder(_TARGET)
        with patch("requests.post", return_value=_response([[1.0, 2.0]])) as post:
            out = e.encode(["", "beta", ""])
        assert out.shape == (3, 2)
        assert out[0].tolist() == [0.0, 0.0]
        assert out[1].tolist() == [1.0, 2.0]
        assert post.call_args.kwargs["json"]["input"] == ["beta"]

    def test_no_texts_makes_no_request(self):
        e = EndpointEmbedder(_TARGET)
        with patch("requests.post") as post:
            out = e.encode([])
        assert out.shape == (0, 0)
        post.assert_not_called()

    def test_batches_large_inputs(self):
        e = EndpointEmbedder(_TARGET)
        texts = [f"t{i}" for i in range(200)]
        with patch("requests.post") as post:
            post.side_effect = lambda *a, **k: _response(
                [[float(i), 1.0] for i in range(len(k["json"]["input"]))]
            )
            out = e.encode(texts)
        assert out.shape == (200, 2)
        assert post.call_count > 1, "200 texts must not go in one request"

    def test_the_model_is_sent(self):
        e = EndpointEmbedder(_TARGET)
        with patch("requests.post", return_value=_response([[1.0]])) as post:
            e.encode(["x"])
        assert post.call_args.kwargs["json"]["model"] == "bge-large-en"

    def test_network_failure_is_wrapped_with_the_target(self):
        e = EndpointEmbedder(_TARGET)
        with patch("requests.post", side_effect=OSError("connection reset")):
            with pytest.raises(InfrastructureError) as exc:
                e.encode(["x"])
        blob = f"{exc.value} {getattr(exc.value, 'detail', '')}"
        assert "connection reset" in blob and "bge-large-en" in blob

    def test_short_response_is_an_error_not_silent_truncation(self):
        """Fewer vectors than texts would misalign every downstream pair."""
        e = EndpointEmbedder(_TARGET)
        with patch("requests.post", return_value=_response([[1.0]])):
            with pytest.raises(InfrastructureError):
                e.encode(["a", "b"])

    def test_the_api_key_is_never_in_the_error(self):
        e = EndpointEmbedder(_TARGET)
        with patch("requests.post", side_effect=OSError("boom")):
            with pytest.raises(InfrastructureError) as exc:
                e.encode(["x"])
        assert "k" != getattr(exc.value, "detail", "")  # sanity
        assert "Bearer" not in f"{exc.value} {getattr(exc.value, 'detail', '')}"


class TestResolveEmbedder:
    """There is no in-process fallback to fall back to."""

    def test_returns_the_endpoint_embedder_when_configured(self, monkeypatch):
        monkeypatch.setenv("ONTOBRICKS_LLM_BASE_URL", "https://x/v1")
        monkeypatch.setenv("ONTOBRICKS_EMBEDDING_MODEL", "bge-large-en")
        assert isinstance(resolve_embedder(), EndpointEmbedder)

    def test_unconfigured_names_the_variable_to_set(self, monkeypatch):
        """Previously this silently returned a torch-backed embedder that then
        failed on encode. The failure now happens at resolve time and says why."""
        monkeypatch.delenv("ONTOBRICKS_EMBEDDING_MODEL", raising=False)
        monkeypatch.setenv("ONTOBRICKS_LLM_BASE_URL", "https://x/v1")
        with pytest.raises(ValidationError) as exc:
            resolve_embedder()
        assert "ONTOBRICKS_EMBEDDING_MODEL" in str(exc.value)

    def test_a_chat_model_alone_does_not_imply_embeddings(self, monkeypatch):
        """A chat model cannot serve /embeddings; guessing yields a provider 404
        that reads as if the endpoint were broken."""
        monkeypatch.setenv("ONTOBRICKS_LLM_BASE_URL", "https://x/v1")
        monkeypatch.setenv("ONTOBRICKS_LLM_MODEL", "some-chat-model")
        monkeypatch.delenv("ONTOBRICKS_EMBEDDING_MODEL", raising=False)
        with pytest.raises(ValidationError):
            resolve_embedder()


class TestNoTorchAnywhere:
    """The point of removing the fallback: nothing imports PyTorch."""

    @pytest.mark.parametrize("module", ["sentence_transformers", "torch", "sklearn"])
    def test_pitfalls_package_never_imports(self, module):
        from pathlib import Path

        pkg = Path("src/back/core/external/pitfalls")
        offenders = [
            f"{f.name}:{n}"
            for f in pkg.glob("*.py")
            for n, line in enumerate(f.read_text().splitlines(), 1)
            if re.match(rf"\s*(import|from)\s+{module}\b", line)
        ]
        assert not offenders, offenders

    def test_local_embedder_is_gone(self):
        from pathlib import Path

        src = Path("src/back/core/external/pitfalls/embedders.py").read_text()
        assert "LocalEmbedder" not in src


class TestRunnerUsesTheSeam:
    """The point of the change: importing the runner must not pull PyTorch."""

    def test_runner_does_not_import_sentence_transformers(self):
        from pathlib import Path

        src = Path("src/back/core/external/pitfalls/runner.py").read_text()
        assert "from sentence_transformers" not in src
        assert "from sklearn" not in src

    def test_runner_resolves_an_embedder_rather_than_a_model(self):
        from pathlib import Path

        src = Path("src/back/core/external/pitfalls/runner.py").read_text()
        assert "_get_model" not in src
        assert "resolve_embedder" in src


class TestRunnerCallsTheEmbedderCorrectly:
    """Exercise the wiring, not just its shape.

    ``_get_embedder`` called ``resolve_embedder(self.model_name)`` after
    ``resolve_embedder`` had lost that parameter — a TypeError on the first
    semantic check. Nothing caught it: the shape tests only read source, and the
    new per-check isolation would have recorded it as ``{"skipped": true}``,
    turning a plain bug into a silently missing check.
    """

    def test_get_embedder_takes_no_arguments_it_cannot_pass(self, monkeypatch):
        import inspect

        from back.core.external.pitfalls import embedders

        assert not [
            p
            for p in inspect.signature(embedders.resolve_embedder).parameters.values()
            if p.default is inspect.Parameter.empty
        ], "resolve_embedder must be callable with no arguments"

    def test_the_runner_resolves_an_embedder_without_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ONTOBRICKS_LLM_BASE_URL", "https://x/v1")
        monkeypatch.setenv("ONTOBRICKS_EMBEDDING_MODEL", "bge-large-en")
        ttl = tmp_path / "o.ttl"
        ttl.write_text(
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            "<http://e/A> a owl:Class .\n"
        )
        from back.core.external.pitfalls.runner import OntologyPatternToolkit

        toolkit = OntologyPatternToolkit(str(ttl))
        embedder = toolkit._get_embedder()
        assert isinstance(embedder, EndpointEmbedder)

    def test_a_failing_check_does_not_lose_the_others(self, monkeypatch, tmp_path):
        """Per-check isolation: an unconfigured endpoint costs four checks, not
        the whole analysis."""
        monkeypatch.delenv("ONTOBRICKS_EMBEDDING_MODEL", raising=False)
        ttl = tmp_path / "o.ttl"
        ttl.write_text(
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
            "<http://e/A> a owl:Class .\n"
            "<http://e/B> a owl:Class ; rdfs:subClassOf <http://e/A> .\n"
        )
        from back.core.external.pitfalls.runner import OntologyPatternToolkit

        results = OntologyPatternToolkit(str(ttl)).run_patterns(["P2.2", "P4.2"])
        assert set(results) == {"P2.2", "P4.2"}
        assert results["P2.2"].get("skipped") is not True, "P2.2 needs no embeddings"
        assert results["P4.2"].get("skipped") is True
        assert "ONTOBRICKS_EMBEDDING_MODEL" in results["P4.2"]["error"]
