"""Reuse an unchanged entity preview instead of re-querying.

Switching between already-mapped entities reran the preview query every time and
showed a spinner for a result that could not have changed. Reimplemented from
upstream 80d096a7, which caches inside a Mapping Designer redesign this fork does
not carry.

Two properties matter more than the caching itself:

* **Only the automatic load on panel open is served from cache.** An explicit Run
  or Refresh click is the user asking for fresh data, so it must bypass. That is
  also why a stale preview is not a correctness problem — the SQL and row limit are
  part of the key, so any edit misses.
* **Only successes are cached.** A failed query must be retried, not remembered.

Static checks, because the frontend has no JS test runner — the same approach as
the sibling manual-mapping tests.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_JS = (
    Path(__file__).resolve().parents[3]
    / "src/front/static/mapping/js/mapping-design.js"
)


def _source() -> str:
    return _JS.read_text()


def _function_body(name: str) -> str:
    """The brace block of *name*, skipping its parameter list.

    Note the `) {` anchor rather than the first `{`: `runEntityPanelQuery(options
    = {})` has braces in a default argument, and matching those returned a
    two-character body that made six of these tests fail for the wrong reason.
    """
    src = _source()
    start = src.index(name)
    depth, i = 0, src.index(") {", start) + 2
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i : j + 1]
    raise AssertionError(f"unbalanced braces after {name}")


class TestTheCacheExists:
    def test_it_is_defined(self):
        assert "const EntityPreviewCache" in _source()

    def test_the_key_includes_uri_sql_and_limit(self):
        """A key missing any of the three serves the wrong rows: a different
        entity, an edited query, or a different row count."""
        body = _function_body("    key(uri, sql, limit)")
        for part in ("uri", "sql", "limit"):
            assert part in body

    def test_it_is_bounded(self):
        """An unbounded Map over a long session is a leak."""
        src = _source()
        assert "_MAX" in src
        block = src[src.index("const EntityPreviewCache") :]
        block = block[: block.index("\n};")]
        assert re.search(r"while\s*\(this\._entries\.size\s*>\s*this\._MAX\)", block)


class TestOnlyTheAutomaticLoadIsServed:
    def test_the_cache_read_is_guarded_by_autoload(self):
        body = _function_body("async function runEntityPanelQuery")
        read = body[body.index("EntityPreviewCache.get") - 300 : body.index("EntityPreviewCache.get")]
        assert "options.autoLoad" in read, (
            "an explicit Run/Refresh must re-query, not reuse a cached preview"
        )

    def test_the_cache_hit_returns_before_the_spinner(self):
        """The complaint was the spinner, not the latency — returning after it
        would leave the visible symptom in place."""
        body = _function_body("async function runEntityPanelQuery")
        hit = body.index("EntityPreviewCache.get")
        spinner = body.index("Refreshing...")
        assert hit < spinner

    def test_a_hit_makes_no_request(self):
        body = _function_body("async function runEntityPanelQuery")
        segment = body[body.index("EntityPreviewCache.get") : body.index("Refreshing...")]
        assert "fetch(" not in segment


class TestOnlySuccessesAreCached:
    def test_the_write_is_inside_the_success_branch(self):
        body = _function_body("async function runEntityPanelQuery")
        write = body.index("EntityPreviewCache.set")
        success = body.index("if (result.success)")
        failure = body.index("} else {", success)
        assert success < write < failure, "a failed query must not be remembered"


class TestHitAndFetchLandIdentically:
    """Duplicating the apply logic is how the two paths drift apart."""

    def test_both_paths_call_one_renderer(self):
        body = _function_body("async function runEntityPanelQuery")
        assert body.count("applyEntityPreviewResult(") == 2

    def test_the_renderer_owns_the_state_writes(self):
        body = _function_body("function applyEntityPreviewResult")
        for expected in (
            "EntityPanelState.columns",
            "EntityPanelState.rows",
            "renderEntityPanelGrid()",
            "epMappingGrid",
        ):
            assert expected in body

    def test_the_fetch_path_no_longer_writes_state_itself(self):
        """If it does, the cache hit and the fetch can diverge silently."""
        body = _function_body("async function runEntityPanelQuery")
        after_success = body[body.index("if (result.success)") : body.index("} else {", body.index("if (result.success)"))]
        assert "EntityPanelState.columns =" not in after_success
