"""``ensure_nltk_resource`` reports failure instead of hiding it.

Why this file exists
--------------------
WordNet and the VADER lexicon are **data**, not code: ``pip install nltk`` does
not bring them, and NLTK fetches them from ``raw.githubusercontent.com`` on first
use. Any environment with restricted egress — a hardened container, CI, a
sandbox — cannot do that at runtime.

The old implementation was::

    try:
        nltk.data.find(resource_path)
    except LookupError:
        nltk.download(download_name, quiet=True)   # returns False on failure

``nltk.download`` returns a bool that was ignored, so a blocked download looked
like success. The function whose whole job is to *ensure* a resource never
checked that it had, and the caller then died one line later on a twenty-line
``LookupError`` about search paths — which says nothing about what to do.

These tests pin the contract: verify, and when it cannot be satisfied say which
corpus and how to pre-seed it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from back.core.errors import InfrastructureError

pytestmark = pytest.mark.unit

nltk = pytest.importorskip("nltk", reason="pitfalls extra not installed")

from back.core.external.pitfalls.utils import ensure_nltk_resource  # noqa: E402


class TestAlreadyPresent:
    def test_returns_without_downloading(self):
        with patch("nltk.data.find") as find, patch("nltk.download") as dl:
            ensure_nltk_resource("corpora/wordnet", "wordnet")
        find.assert_called_once()
        dl.assert_not_called()


class TestDownloadSucceeds:
    def test_second_find_confirms_it(self):
        with patch("nltk.data.find", side_effect=[LookupError, None]) as find, \
             patch("nltk.download", return_value=True):
            ensure_nltk_resource("corpora/wordnet", "wordnet")
        assert find.call_count == 2, "must verify, not assume"


class TestDownloadFails:
    def test_silent_failure_raises(self):
        """The exact old bug: download returns False, resource still absent."""
        with patch("nltk.data.find", side_effect=LookupError), \
             patch("nltk.download", return_value=False):
            with pytest.raises(InfrastructureError):
                ensure_nltk_resource("corpora/wordnet", "wordnet")

    def test_error_names_the_corpus(self):
        with patch("nltk.data.find", side_effect=LookupError), \
             patch("nltk.download", return_value=False):
            with pytest.raises(InfrastructureError) as exc:
                ensure_nltk_resource("corpora/wordnet", "wordnet")
        assert "wordnet" in str(exc.value)

    def test_error_says_how_to_pre_seed(self):
        with patch("nltk.data.find", side_effect=LookupError), \
             patch("nltk.download", return_value=False):
            with pytest.raises(InfrastructureError) as exc:
                ensure_nltk_resource("sentiment/vader_lexicon", "vader_lexicon")
        detail = f"{exc.value} {getattr(exc.value, 'detail', '')}"
        assert "nltk.downloader" in detail

    def test_network_error_is_wrapped_not_leaked(self):
        """A urlopen failure is what a blocked host actually raises."""
        with patch("nltk.data.find", side_effect=LookupError), \
             patch("nltk.download", side_effect=OSError("refusing to connect")):
            with pytest.raises(InfrastructureError) as exc:
                ensure_nltk_resource("corpora/wordnet", "wordnet")
        assert "refusing to connect" in f"{exc.value} {getattr(exc.value, 'detail', '')}"


class TestPathGuardStillApplies:
    """The CVE-2026-54293 defence must run before any download."""

    @pytest.mark.parametrize("bad", ["corpora/%2e%2e/etc", "../etc/passwd", "/etc/passwd"])
    def test_unsafe_paths_are_rejected(self, bad):
        with patch("nltk.download") as dl:
            with pytest.raises(ValueError):
                ensure_nltk_resource(bad, "wordnet")
        dl.assert_not_called()


class TestEveryWordnetUserEnsuresFirst:
    """A missing ensure_nltk_resource call is how the opaque error came back."""

    def test_no_unguarded_nltk_import_in_the_runner(self):
        """Every *runtime* nltk import must be preceded by an ensure call.

        Parsed rather than line-scanned, and ``if TYPE_CHECKING:`` blocks are
        skipped: those imports never execute, so they cannot raise a corpus
        error. The first version of this test scanned raw lines and flagged the
        annotation-only ``SentimentIntensityAnalyzer`` import that upstream
        362e40af added to satisfy F821 — a true positive for the rule as written,
        and the rule was wrong.
        """
        import ast
        from pathlib import Path

        src = Path("src/back/core/external/pitfalls/runner.py").read_text()
        tree = ast.parse(src)

        type_checking_lines: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                test = node.test
                name = getattr(test, "id", None) or getattr(test, "attr", None)
                if name == "TYPE_CHECKING":
                    for child in ast.walk(node):
                        if hasattr(child, "lineno"):
                            type_checking_lines.add(child.lineno)

        lines = src.splitlines()
        unguarded = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("nltk")
            and node.lineno not in type_checking_lines
            and "ensure_nltk_resource"
            not in "\n".join(lines[max(0, node.lineno - 7) : node.lineno - 1])
        ]
        assert not unguarded, f"nltk imported without ensuring the corpus at {unguarded}"
