"""Guard the JS against stale canonical values.

The frontend has no test runner, and that gap has already cost once: after the
backend started emitting ``graph_engine: "postgres"`` (P3d), roughly 25 JS sites
still compared against ``'lakebase'``. Those branches simply never fired — the
PostgreSQL panel would not render — while the entire Python suite stayed green.
It was found by grepping, not by a test.

These are deliberately crude static checks, not a JS test suite. They encode the
one invariant that matters: a value the backend no longer sends must not be the
*sole* thing the frontend compares against, and defaults must be canonical.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_STATIC = Path(__file__).resolve().parents[3] / "src" / "front" / "static"
_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "front" / "templates"

#: Values the backend can send for an engine / backend field.
_CANONICAL = "postgres"
_LEGACY = "lakebase"

#: Lines where ``'lakebase'`` is a *sidebar section id*, not an engine value —
#: the Settings panel is still called "Lakebase" in the navigation. Allowlisted
#: rather than loosened, so any genuinely new engine-value comparison still
#: fails. Renaming the section is a UI-copy change, not a correctness one.
_SECTION_ID_USES = {
    ("config/js/settings.js", "if (s === 'lakebase' || s === 'delta') {"),
    ("config/js/settings.js", "} else if (s === 'lakebase') {"),
}


def _js_files():
    return sorted(_STATIC.rglob("*.js"))


def _lines_with(pattern: str):
    """Yield ``(path, lineno, line)`` for JS lines matching *pattern*."""
    rx = re.compile(pattern)
    for path in _js_files():
        for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if rx.search(line):
                yield path, num, line.strip()


class TestNoLegacyOnlyDefaults:
    def test_no_js_defaults_to_the_legacy_engine_value(self):
        """``x || 'lakebase'`` would silently mislabel every engine.

        The backend sends ``postgres``; a legacy default only takes effect when
        the field is absent, and then it is wrong.
        """
        offenders = [
            f"{p.relative_to(_STATIC)}:{n}: {ln}"
            for p, n, ln in _lines_with(r"\|\|\s*'lakebase'")
        ]
        assert offenders == [], (
            "these default to the retired engine value instead of "
            f"'{_CANONICAL}':\n  " + "\n  ".join(offenders)
        )

    def test_no_js_compares_only_against_the_legacy_value(self):
        """``eng === 'lakebase'`` is a branch that can never be taken."""
        offenders = []
        for p, n, ln in _lines_with(r"===\s*'lakebase'|'lakebase'\s*==="):
            # A line that also mentions the canonical value is doing a
            # both-spellings comparison, which is the correct migration shape.
            if _CANONICAL in ln:
                continue
            rel = f"{p.relative_to(_STATIC)}"
            if any(
                rel == a_file and ln == a_line for a_file, a_line in _SECTION_ID_USES
            ):
                continue
            offenders.append(f"{rel}:{n}: {ln}")
        assert offenders == [], (
            "these compare only against the retired value, so the branch is "
            "dead:\n  " + "\n  ".join(offenders)
        )


class TestCanonicalIsPresent:
    def test_engine_label_maps_include_postgres(self):
        """A label map keyed only by the legacy value renders no label."""
        maps = [
            p for p in _js_files() if "'lakebase':" in p.read_text(encoding="utf-8")
        ]
        for path in maps:
            text = path.read_text(encoding="utf-8")
            assert "'postgres':" in text, (
                f"{path.relative_to(_STATIC)} keys an engine map by "
                f"'{_LEGACY}' but not '{_CANONICAL}'"
            )

    def test_domain_backend_select_offers_postgres(self):
        """The stored value written by the picker must be canonical."""
        html = (
            _TEMPLATES / "partials" / "domain" / "_domain_information.html"
        ).read_text(encoding="utf-8")
        assert 'value="postgres"' in html
        assert (
            'value="lakebase"' not in html
        ), "the picker would write the retired value into domain JSON"


class TestSyntax:
    """Parse the JS with node when it is available.

    Nothing else compiles these files, so a botched block excision would ship.
    A hand-rolled brace counter was tried first and gave false positives on
    regex literals — ``node --check`` is an actual parser, so use it and skip
    honestly when node is absent rather than approximate it.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "config/js/settings.js",
            "config/js/settings-registry-configuration.js",
            "query/js/query-sync.js",
            "query/js/query-databricks-build.js",
            "domain/js/domain.js",
            "domain/js/domain-validation.js",
            "registry/js/registry.js",
            "global/js/runs-render.js",
        ],
    )
    def test_parses(self, name):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            pytest.skip("node not on PATH — cannot parse JS")
        path = _STATIC / name
        result = subprocess.run(
            [node, "--check", str(path)], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, f"{name} failed to parse:\n{result.stderr}"


class TestLLMRequestFieldNames:
    """The JS request body and the route that reads it must agree.

    P3d shipped ~25 JS sites comparing against a value the backend had stopped
    sending, with the whole Python suite green — a field-name change on one side
    of an HTTP boundary is invisible to tests that only exercise one side. P6's
    second revision renamed the SQL Wizard's ``endpoint_name`` field to
    ``model``, which is the same hazard, so it gets the same guard.
    """

    _ROUTE = Path(__file__).resolve().parents[3] / "src/api/routers/internal/mapping.py"

    def test_no_js_still_sends_the_retired_endpoint_name_field(self):
        offenders = [
            f"{p.relative_to(_STATIC)}:{n}"
            for p in _STATIC.rglob("*.js")
            for n, line in enumerate(p.read_text().splitlines(), 1)
            if "endpoint_name:" in line
        ]
        assert not offenders, (
            "JS still sends the retired 'endpoint_name' field; the route reads "
            f"'model': {offenders}"
        )

    def test_the_generate_sql_route_reads_model(self):
        assert 'data.get("model")' in self._ROUTE.read_text()

    def test_the_generate_sql_route_no_longer_reads_endpoint_name(self):
        assert 'data.get("endpoint_name")' not in self._ROUTE.read_text()

    def test_js_sends_the_field_the_route_reads(self):
        wizard_js = (_STATIC / "mapping/js/mapping-shared.js").read_text()
        assert "model: document.getElementById" in wizard_js
