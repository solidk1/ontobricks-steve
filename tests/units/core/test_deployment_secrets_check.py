"""The readiness probe for deployment-unsafe defaults.

Every field in ``Settings`` has a default, so OntoBricks starts happily with
nothing configured — correct for local development, dangerous in a container.
``SECRET_KEY`` in particular falls back to a literal published in this repository,
which means session cookies signed with a key anyone can read. Nothing surfaced
that before this probe existed, so these tests pin the severity in each state.
"""

import pytest

from shared.fastapi import health
from shared.config.settings import Settings

pytestmark = pytest.mark.unit

_DEPLOYED = {"ONTOBRICKS_CONTAINERIZED": "true"}
_SAFE = {
    "ONTOBRICKS_CONTAINERIZED": "true",
    "ONTOBRICKS_SECURE_COOKIES": "true",
    "ONTOBRICKS_AUTH_ENABLED": "true",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "ONTOBRICKS_CONTAINERIZED",
        "ONTOBRICKS_SECURE_COOKIES",
        "ONTOBRICKS_AUTH_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)


def _run(monkeypatch, env, secret):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return health._check_deployment_secrets(Settings(secret_key=secret))


class TestSecretKey:
    def test_default_secret_is_only_a_warning_locally(self, monkeypatch):
        status, _ = _run(monkeypatch, {}, health._DEFAULT_SECRET_KEY)
        assert status == health._WARNING

    def test_default_secret_is_an_error_once_deployed(self, monkeypatch):
        status, detail = _run(monkeypatch, _DEPLOYED, health._DEFAULT_SECRET_KEY)
        assert status == health._ERROR
        assert "SECRET_KEY" in detail

    def test_the_default_is_read_off_the_field_not_restated(self):
        """Guards against this probe drifting from ``Settings``."""
        assert health._DEFAULT_SECRET_KEY == Settings.model_fields["secret_key"].default

    def test_a_real_secret_passes_locally(self, monkeypatch):
        status, _ = _run(monkeypatch, {}, "a-real-random-value")
        assert status == health._OK


class TestDeployedOnlyChecks:
    def test_non_tls_cookies_are_an_error_once_deployed(self, monkeypatch):
        status, detail = _run(monkeypatch, _DEPLOYED, "a-real-random-value")
        assert status == health._ERROR
        assert "ONTOBRICKS_SECURE_COOKIES" in detail

    def test_disabled_auth_is_an_error_once_deployed(self, monkeypatch):
        env = dict(_SAFE, ONTOBRICKS_AUTH_ENABLED="false")
        status, detail = _run(monkeypatch, env, "a-real-random-value")
        assert status == health._ERROR
        assert "ONTOBRICKS_AUTH_ENABLED" in detail

    def test_cookie_and_auth_flags_are_not_checked_locally(self, monkeypatch):
        """A developer running `make run` must not be nagged about TLS."""
        status, _ = _run(monkeypatch, {}, "a-real-random-value")
        assert status == health._OK

    def test_a_correctly_configured_deployment_passes(self, monkeypatch):
        status, _ = _run(monkeypatch, _SAFE, "a-real-random-value")
        assert status == health._OK


class TestWiring:
    def test_the_probe_is_registered_in_the_readiness_run(self, monkeypatch):
        monkeypatch.setattr(health, "_build_health_client", lambda *a, **k: None)
        ids = {c["name"] for c in health.run_readiness_checks()["checks"]}
        assert "deployment.secrets" in ids
