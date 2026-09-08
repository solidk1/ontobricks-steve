"""A Databricks-free deployment must not report ``/health`` status "error".

Why this file exists
--------------------
``databricks.auth`` returned ``error`` whenever credentials were unusable,
which included the case where none were configured at all. Once OntoBricks
could run on a plain container plus a plain Postgres — no workspace anywhere
— that made the top-level ``status`` field permanently ``"error"`` on a fully
healthy, fully supported deployment.

That is worse than cosmetic. ``deploy/azure/Dockerfile`` tells operators to
read the JSON ``status`` rather than the HTTP code, so a field that can never
go green makes the documented monitoring signal useless.

The distinction the check now draws: *nothing* configured is a supported
choice (warning), *something* configured but unusable is a misconfiguration
(error).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from shared.fastapi.health import _check_databricks_auth

pytestmark = pytest.mark.unit

_INTENT = (
    "DATABRICKS_HOST",
    "DATABRICKS_CLIENT_ID",
    "DATABRICKS_CLIENT_SECRET",
    "DATABRICKS_TOKEN",
    "DATABRICKS_CONFIG_PROFILE",
)


@pytest.fixture
def no_databricks_env(monkeypatch):
    for var in _INTENT:
        monkeypatch.delenv(var, raising=False)
    yield monkeypatch


def _unusable():
    """Patch DatabricksAuth so ``has_valid_auth()`` is False."""
    return patch(
        "back.core.databricks.DatabricksAuth.DatabricksAuth",
        return_value=MagicMock(
            has_valid_auth=MagicMock(return_value=False), auth_mode="none"
        ),
    )


class TestAbsentDatabricksIsNotAnError:
    def test_nothing_configured_warns(self, no_databricks_env):
        with _unusable():
            status, _ = _check_databricks_auth()
        assert status == "warning"

    def test_nothing_configured_says_it_is_optional(self, no_databricks_env):
        with _unusable():
            _, detail = _check_databricks_auth()
        assert "optional" in detail.lower()

    def test_nothing_configured_names_what_is_unavailable(self, no_databricks_env):
        """An operator must learn which features they gave up, not just that
        something is missing."""
        with _unusable():
            _, detail = _check_databricks_auth()
        assert "Volume" in detail and "warehouse" in detail

    def test_nothing_configured_does_not_demand_credentials(self, no_databricks_env):
        """The old copy read as an instruction; it is now a statement of fact."""
        with _unusable():
            _, detail = _check_databricks_auth()
        assert "Set DATABRICKS_CLIENT_ID" not in detail


class TestPartialDatabricksIsStillAnError:
    @pytest.mark.parametrize("var", _INTENT)
    def test_any_intent_variable_makes_it_an_error(self, no_databricks_env, var):
        no_databricks_env.setenv(var, "something")
        with _unusable():
            status, detail = _check_databricks_auth()
        assert status == "error"
        assert var in detail, "the error must name the variable that was set"

    def test_error_says_how_to_opt_out(self, no_databricks_env):
        no_databricks_env.setenv("DATABRICKS_HOST", "https://example.invalid")
        with _unusable():
            _, detail = _check_databricks_auth()
        assert "Unset" in detail
