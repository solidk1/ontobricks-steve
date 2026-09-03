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

    def test_legacy_app_port_is_ignored(self, monkeypatch):
        """The Apps deploy is gone; only PORT is honoured."""
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.delenv("PORT", raising=False)
        monkeypatch.setenv("DATABRICKS_APP_PORT", "8501")
        assert RuntimeEnv.port() == 8000

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

    def test_legacy_apps_probe_no_longer_implies_containerized(self, monkeypatch):
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.delenv("ONTOBRICKS_CONTAINERIZED", raising=False)
        monkeypatch.setenv("DATABRICKS_APP_PORT", "8501")
        assert RuntimeEnv.is_containerized() is False


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

    def test_explicit_false_wins(self, monkeypatch):
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.setenv("ONTOBRICKS_AUTH_ENABLED", "false")
        assert RuntimeEnv.auth_enabled() is False

    def test_unset_fails_closed(self, monkeypatch):
        """A deployment that configures nothing must be locked, not open.

        This flipped in P4b: before OIDC login existed, defaulting to on would
        have locked out local development with no way back in.
        """
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.delenv("ONTOBRICKS_AUTH_ENABLED", raising=False)

        monkeypatch.delenv("DATABRICKS_APP_PORT", raising=False)
        assert RuntimeEnv.auth_enabled() is True

        monkeypatch.setenv("DATABRICKS_APP_PORT", "8501")
        assert RuntimeEnv.auth_enabled() is True

    def test_explicitly_disabled_for_local_dev(self, monkeypatch):
        from shared.config.RuntimeEnv import RuntimeEnv

        monkeypatch.setenv("ONTOBRICKS_AUTH_ENABLED", "false")
        assert RuntimeEnv.auth_enabled() is False


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


class TestDatabricksAppPortIsGone:
    """``DATABRICKS_APP_PORT`` must not be read anywhere.

    The Databricks Apps deploy has been removed, so the variable means
    nothing. Reintroducing a read would quietly resurrect platform-specific
    behaviour that nothing else in the codebase accounts for.
    """

    def test_no_readers(self):
        import pathlib
        import re

        src = pathlib.Path(__file__).resolve().parents[3] / "src"
        offenders = []
        for path in src.rglob("*.py"):
            for num, line in enumerate(path.read_text().splitlines(), 1):
                if "DATABRICKS_APP_PORT" not in line:
                    continue
                # Prose in docstrings/comments is fine; code reads are not.
                if re.search(r"(getenv|environ)", line):
                    offenders.append(f"{path.relative_to(src)}:{num}")
        assert offenders == [], (
            "DATABRICKS_APP_PORT is retired along with the Databricks Apps "
            f"deploy; found reads in: {offenders}"
        )
