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


class TestLLMUICopy:
    """The UI must not tell users the LLM has to be a Databricks endpoint.

    It no longer does, and the label drift is invisible to every other test: the
    picker keeps working, the routes keep passing, and the only thing wrong is
    what the user is told. That is precisely the kind of breakage that survives a
    green suite, so it gets asserted.
    """

    _TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "front" / "templates"

    # Text that was accurate while Databricks was the only supported provider.
    _STALE = (
        "Databricks Model Serving endpoint",
        "must be a Databricks Model Serving",
    )

    def test_no_template_claims_the_llm_must_be_a_databricks_endpoint(self):
        offenders = [
            f"{p.relative_to(self._TEMPLATES)}:{n}"
            for p in self._TEMPLATES.rglob("*.html")
            for n, line in enumerate(p.read_text().splitlines(), 1)
            if any(t in line for t in self._STALE)
        ]
        assert not offenders, f"stale provider claim in templates: {offenders}"

    def test_no_js_claims_the_llm_must_be_a_databricks_endpoint(self):
        offenders = [
            f"{p.relative_to(_STATIC)}:{n}"
            for p in _STATIC.rglob("*.js")
            for n, line in enumerate(p.read_text().splitlines(), 1)
            if any(t in line for t in self._STALE)
        ]
        assert not offenders, f"stale provider claim in JS: {offenders}"

    def test_the_settings_tab_names_the_provider_variables(self):
        """An operator seeing an empty picker needs to know what to set."""
        tab = (self._TEMPLATES / "partials/domain/_domain_information.html").read_text()
        assert "ONTOBRICKS_LLM_BASE_URL" in tab
        assert "ONTOBRICKS_LLM_MODELS" in tab

    @pytest.mark.parametrize(
        "name",
        ["domain/js/domain-information.js", "global/js/utils.js"],
    )
    def test_empty_picker_says_what_to_configure(self, name):
        js = (_STATIC / name).read_text()
        assert "ONTOBRICKS_LLM_MODELS" in js, (
            f"{name}: an empty model list must name the variable to set, not just "
            "say 'no endpoints available'"
        )


class TestRetiredDeploymentCopy:
    """The UI must not instruct operators to set variables that no longer exist.

    The Registry panel told users to set ``LAKEBASE_SCHEMA`` and to "bind the
    Volume and Lakebase resources in ``app.yaml``" long after both were retired
    (``ONTOBRICKS_PG_SCHEMA`` replaced the former; the asset bundle and
    ``app.yaml`` were deleted in v0.7.1). On the first non-Databricks deployment
    that message was the *only* thing the operator saw, and every instruction in
    it was wrong.

    Nothing else can catch this: the panel renders, the fetch succeeds, and the
    copy is simply false.
    """

    #: Names retired from the runtime, mapped to what replaced them.
    _RETIRED = {
        "LAKEBASE_SCHEMA": "ONTOBRICKS_PG_SCHEMA",
        "app.yaml": "the container environment",
        "REGISTRY_VOLUME_PATH": "PGHOST/PGDATABASE",
    }

    def _offenders(self, needle: str, suffix: str, root: Path):
        return [
            f"{p.relative_to(root)}:{n}"
            for p in root.rglob(suffix)
            for n, line in enumerate(p.read_text().splitlines(), 1)
            if needle in line
        ]

    @pytest.mark.parametrize("retired", sorted(_RETIRED))
    def test_no_js_instructs_setting_a_retired_variable(self, retired):
        offenders = self._offenders(retired, "*.js", _STATIC)
        assert not offenders, (
            f"{retired} is retired (use {self._RETIRED[retired]}) but is still "
            f"named in JS: {offenders}"
        )

    @pytest.mark.parametrize("retired", ["LAKEBASE_SCHEMA"])
    def test_no_template_instructs_setting_a_retired_variable(self, retired):
        offenders = self._offenders(retired, "*.html", _TEMPLATES)
        assert not offenders, f"{retired} still named in templates: {offenders}"

    def test_registry_panel_names_the_postgres_variables(self):
        """A registry with no Postgres must say which variables to set."""
        js = (_STATIC / "registry/js/registry.js").read_text()
        for var in ("PGHOST", "PGDATABASE", "ONTOBRICKS_PG_SCHEMA"):
            assert var in js, f"registry.js must name {var} when unconfigured"

    def test_registry_panel_does_not_blame_the_volume_when_uninitialized(self):
        """The Volume is optional; 'create the volume' is not the fix."""
        js = (_STATIC / "registry/js/registry.js").read_text()
        assert "Initialize</strong> to create the volume" not in js

    def test_registry_defaults_match_the_backend_payload(self):
        """``as_dict()`` emits postgres_*; the JS defaults must not shadow it."""
        js = (_STATIC / "registry/js/registry.js").read_text()
        head = js[: js.index("loadRegistryConfig();")]
        assert "postgres_schema:" in head
        assert "lakebase_schema:" not in head
