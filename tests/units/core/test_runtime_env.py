"""Tests for shared.config.RuntimeEnv — the runtime-predicate split.

``DatabricksAuth.is_databricks_app()`` used to answer seven unrelated
questions with one boolean (port, filesystem, cookie security, auth
enforcement, credential resolution, setting lock, resource caps).
:class:`RuntimeEnv` owns the first four; the rest live with their own
subjects.

Every predicate here is *behaviour-preserving* against the legacy
``DATABRICKS_APP_PORT`` probe, so P1 is a pure refactor. ``auth_enabled``
flips to default-on in P4, once an OIDC login flow exists to satisfy it.
"""

import pytest

pytestmark = pytest.mark.unit


class TestPort:
    def test_defaults_to_8000(self, monkeypatch):
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.delenv("PORT", raising=False)
        monkeypatch.delenv("DATABRICKS_APP_PORT", raising=False)
        assert RuntimeEnv.port() == 8000

    def test_reads_port(self, monkeypatch):
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.setenv("PORT", "9001")
        assert RuntimeEnv.port() == 9001

    def test_port_wins_over_legacy_app_port(self, monkeypatch):
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.setenv("PORT", "9001")
        monkeypatch.setenv("DATABRICKS_APP_PORT", "8501")
        assert RuntimeEnv.port() == 9001

    def test_falls_back_to_legacy_app_port(self, monkeypatch):
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.delenv("PORT", raising=False)
        monkeypatch.setenv("DATABRICKS_APP_PORT", "8501")
        assert RuntimeEnv.port() == 8501

    def test_ignores_non_numeric(self, monkeypatch):
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.setenv("PORT", "not-a-port")
        monkeypatch.delenv("DATABRICKS_APP_PORT", raising=False)
        assert RuntimeEnv.port() == 8000


class TestIsContainerized:
    def test_false_by_default(self, monkeypatch):
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.delenv("ONTOBRICKS_CONTAINERIZED", raising=False)
        monkeypatch.delenv("DATABRICKS_APP_PORT", raising=False)
        assert RuntimeEnv.is_containerized() is False

    def test_explicit_flag_wins(self, monkeypatch):
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.setenv("ONTOBRICKS_CONTAINERIZED", "true")
        monkeypatch.delenv("DATABRICKS_APP_PORT", raising=False)
        assert RuntimeEnv.is_containerized() is True

    def test_explicit_false_overrides_legacy_probe(self, monkeypatch):
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.setenv("ONTOBRICKS_CONTAINERIZED", "false")
        monkeypatch.setenv("DATABRICKS_APP_PORT", "8501")
        assert RuntimeEnv.is_containerized() is False

    def test_legacy_apps_probe_implies_containerized(self, monkeypatch):
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.delenv("ONTOBRICKS_CONTAINERIZED", raising=False)
        monkeypatch.setenv("DATABRICKS_APP_PORT", "8501")
        assert RuntimeEnv.is_containerized() is True


class TestSecureCookies:
    """Independent of auth_enabled — conflating them is the bug this fixes."""

    def test_off_by_default(self, monkeypatch):
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.delenv("ONTOBRICKS_SECURE_COOKIES", raising=False)
        monkeypatch.delenv("DATABRICKS_APP_PORT", raising=False)
        assert RuntimeEnv.secure_cookies() is False

    def test_explicit_flag_wins(self, monkeypatch):
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.setenv("ONTOBRICKS_SECURE_COOKIES", "1")
        monkeypatch.delenv("DATABRICKS_APP_PORT", raising=False)
        assert RuntimeEnv.secure_cookies() is True

    def test_independent_of_auth_enabled(self, monkeypatch):
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.setenv("ONTOBRICKS_SECURE_COOKIES", "false")
        monkeypatch.setenv("ONTOBRICKS_AUTH_ENABLED", "true")
        assert RuntimeEnv.secure_cookies() is False
        assert RuntimeEnv.auth_enabled() is True


class TestAuthEnabled:
    def test_explicit_true(self, monkeypatch):
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.setenv("ONTOBRICKS_AUTH_ENABLED", "true")
        monkeypatch.delenv("DATABRICKS_APP_PORT", raising=False)
        assert RuntimeEnv.auth_enabled() is True

    def test_explicit_false_even_on_legacy_apps(self, monkeypatch):
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.setenv("ONTOBRICKS_AUTH_ENABLED", "false")
        monkeypatch.setenv("DATABRICKS_APP_PORT", "8501")
        assert RuntimeEnv.auth_enabled() is False

    def test_unset_preserves_legacy_behaviour(self, monkeypatch):
        """P1 is behaviour-preserving: unset == the old is_databricks_app().

        P4 flips this default to True (fail closed) once OIDC login exists.
        When that lands, this test is the one that must change.
        """
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.delenv("ONTOBRICKS_AUTH_ENABLED", raising=False)

        monkeypatch.delenv("DATABRICKS_APP_PORT", raising=False)
        assert RuntimeEnv.auth_enabled() is False

        monkeypatch.setenv("DATABRICKS_APP_PORT", "8501")
        assert RuntimeEnv.auth_enabled() is True


class TestBooleanParsing:
    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "True", "yes", "on"])
    def test_truthy(self, monkeypatch, raw):
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.setenv("ONTOBRICKS_AUTH_ENABLED", raw)
        assert RuntimeEnv.auth_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", ""])
    def test_falsy(self, monkeypatch, raw):
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.setenv("ONTOBRICKS_AUTH_ENABLED", raw)
        monkeypatch.setenv("DATABRICKS_APP_PORT", "8501")
        assert RuntimeEnv.auth_enabled() is False


class TestNoLegacyReadersOutsideRuntimeEnv:
    """DATABRICKS_APP_PORT must be read in exactly one place.

    The whole point of the split is that the legacy probe has a single
    home, so P7 can delete it by touching one function. This test fails
    if anyone reintroduces a direct read.
    """

    def test_single_legacy_reader(self):
        import pathlib
        import re

        src = pathlib.Path(__file__).resolve().parents[3] / "src"
        offenders = []
        for path in src.rglob("*.py"):
            if path.name == "RuntimeEnv.py":
                continue
            for num, line in enumerate(path.read_text().splitlines(), 1):
                if "DATABRICKS_APP_PORT" not in line:
                    continue
                # Prose in docstrings/comments is fine; code reads are not.
                if re.search(r"(getenv|environ)", line):
                    offenders.append(f"{path.relative_to(src)}:{num}")
        # src/mcp-server ships its own pyproject/uv.lock and cannot import
        # from src/shared, so it keeps a PORT-first duplicate until P7 folds
        # it into the main app. Any *other* reader is a regression.
        transitional = {"mcp-server/server/app.py"}
        unexpected = [o for o in offenders if o.rsplit(":", 1)[0] not in transitional]
        assert unexpected == [], (
            "DATABRICKS_APP_PORT must only be read by "
            f"shared/config/RuntimeEnv.py, found: {unexpected}"
        )
