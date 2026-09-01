"""Tests for DatabricksConnector — 'can credentials resolve implicitly?'

Eleven call sites used to ask ``is_databricks_app()`` when what they
actually needed to know was whether Databricks credentials can be
resolved without an explicit host/token pair. Inside Databricks Apps
those were the same question; off-platform with a service principal they
are not, which is why the old predicate blocked container deployment.
"""

import pytest

pytestmark = pytest.mark.unit


class TestHasImplicitCredentials:
    def test_true_when_sp_credentials_present(self, monkeypatch):
        from back.core.databricks.DatabricksConnector import DatabricksConnector

        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-id")
        monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "sp-secret")
        assert DatabricksConnector.has_implicit_credentials() is True

    def test_false_when_only_client_id(self, monkeypatch):
        from back.core.databricks.DatabricksConnector import DatabricksConnector

        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-id")
        monkeypatch.delenv("DATABRICKS_CLIENT_SECRET", raising=False)
        assert DatabricksConnector.has_implicit_credentials() is False

    def test_false_when_only_secret(self, monkeypatch):
        from back.core.databricks.DatabricksConnector import DatabricksConnector

        monkeypatch.delenv("DATABRICKS_CLIENT_ID", raising=False)
        monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "sp-secret")
        assert DatabricksConnector.has_implicit_credentials() is False

    def test_false_when_neither(self, monkeypatch):
        from back.core.databricks.DatabricksConnector import DatabricksConnector

        monkeypatch.delenv("DATABRICKS_CLIENT_ID", raising=False)
        monkeypatch.delenv("DATABRICKS_CLIENT_SECRET", raising=False)
        assert DatabricksConnector.has_implicit_credentials() is False

    def test_blank_values_do_not_count(self, monkeypatch):
        from back.core.databricks.DatabricksConnector import DatabricksConnector

        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "   ")
        monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "sp-secret")
        assert DatabricksConnector.has_implicit_credentials() is False

    def test_independent_of_apps_platform(self, monkeypatch):
        """The regression this whole seam exists to prevent.

        A container with SP credentials and no Apps runtime must resolve
        credentials; under the old predicate it could not.
        """
        from back.core.databricks.DatabricksConnector import DatabricksConnector

        monkeypatch.delenv("DATABRICKS_APP_PORT", raising=False)
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-id")
        monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "sp-secret")
        assert DatabricksConnector.has_implicit_credentials() is True


class TestIsConfigured:
    def test_needs_a_host(self, monkeypatch):
        from back.core.databricks.DatabricksConnector import DatabricksConnector

        monkeypatch.delenv("DATABRICKS_HOST", raising=False)
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-id")
        monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "sp-secret")
        assert DatabricksConnector.is_configured() is False

    def test_host_plus_sp_credentials(self, monkeypatch):
        from back.core.databricks.DatabricksConnector import DatabricksConnector

        monkeypatch.setenv("DATABRICKS_HOST", "https://ws.cloud.databricks.com")
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-id")
        monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "sp-secret")
        assert DatabricksConnector.is_configured() is True

    def test_host_plus_pat(self, monkeypatch):
        from back.core.databricks.DatabricksConnector import DatabricksConnector

        monkeypatch.setenv("DATABRICKS_HOST", "https://ws.cloud.databricks.com")
        monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-xxx")
        monkeypatch.delenv("DATABRICKS_CLIENT_ID", raising=False)
        monkeypatch.delenv("DATABRICKS_CLIENT_SECRET", raising=False)
        assert DatabricksConnector.is_configured() is True

    def test_host_without_any_credential(self, monkeypatch):
        from back.core.databricks.DatabricksConnector import DatabricksConnector

        monkeypatch.setenv("DATABRICKS_HOST", "https://ws.cloud.databricks.com")
        monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
        monkeypatch.delenv("DATABRICKS_CLIENT_ID", raising=False)
        monkeypatch.delenv("DATABRICKS_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
        assert DatabricksConnector.is_configured() is False
