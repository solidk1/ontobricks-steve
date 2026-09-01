"""Whether and how the Databricks connector can authenticate.

OntoBricks treats Databricks as an *optional connector*: source-table
reads, the Delta triple-store engine, UC Volume attachments, Lakeview
dashboards and the Foundation Model API all go through it, but the
application runs on its own container and its own Postgres.

Eleven call sites used to ask ``DatabricksAuth.is_databricks_app()`` in
order to decide *"may I proceed without an explicit host and token?"*.
Inside Databricks Apps those questions coincide, because the platform
injects service-principal credentials. Off-platform they do not, and
that conflation is precisely what stopped OntoBricks running in a plain
container with a service principal.

This class answers the credential question directly and says nothing
about where the process happens to run.
"""

from __future__ import annotations

import os


class DatabricksConnector:
    """Credential-resolution facts about the Databricks connector."""

    @staticmethod
    def _env(name: str) -> str:
        return (os.getenv(name) or "").strip()

    @staticmethod
    def has_implicit_credentials() -> bool:
        """Whether credentials resolve without an explicit host/token.

        True when a service principal is configured
        (``DATABRICKS_CLIENT_ID`` + ``DATABRICKS_CLIENT_SECRET``), which
        is what the Apps runtime used to inject and what a container
        deployment supplies from its own secret store.

        Callers use this to decide whether a missing host or token is a
        configuration error or simply something to resolve at call time.
        """
        return bool(
            DatabricksConnector._env("DATABRICKS_CLIENT_ID")
            and DatabricksConnector._env("DATABRICKS_CLIENT_SECRET")
        )

    @staticmethod
    def is_configured() -> bool:
        """Whether the connector can reach a workspace at all.

        Requires a host plus one usable credential: a service principal,
        a personal access token, or a named CLI profile. Connector-gated
        features (UC metadata, the Delta engine, ``ai_parse_document``,
        Lakeview listing) report themselves unavailable when this is
        false, rather than failing at call time.
        """
        if not DatabricksConnector._env("DATABRICKS_HOST"):
            return False
        return bool(
            DatabricksConnector.has_implicit_credentials()
            or DatabricksConnector._env("DATABRICKS_TOKEN")
            or DatabricksConnector._env("DATABRICKS_CONFIG_PROFILE")
        )
