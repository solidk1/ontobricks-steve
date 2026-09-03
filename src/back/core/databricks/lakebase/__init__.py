"""Shared technical access layer for Lakebase (Postgres).

Single home for the Lakebase connection machinery used by the two
independent Lakebase databases in OntoBricks — the registry store and the
graph triple store. Consumers import the pool / auth / grant primitives from
here and supply their own connection coordinates (auth + schema + database).
"""

from back.core.databricks.lakebase.grants import (  # noqa: F401
    grant_can_use_on_project,
    grant_schema_privileges,
    grant_uc_catalog,
    resolve_app_service_principals,
    resolve_mcp_app_name,
)
from back.core.databricks.lakebase.LakebaseAuth import (  # noqa: F401
    LakebaseAuth,
    get_graph_auth,
    get_lakebase_auth,
)
from back.core.postgres.PostgresConnectionPool import (  # noqa: F401
    PostgresConnectionError,
    PostgresConnectionPool,
    get_postgres_pool,
    postgres_cursor,
)
from back.core.postgres.psycopg_gate import require_psycopg  # noqa: F401

__all__ = [
    # grants
    "resolve_app_service_principals",
    "resolve_mcp_app_name",
    "grant_can_use_on_project",
    "grant_schema_privileges",
    "grant_uc_catalog",
    # connection
    "PostgresConnectionError",
    "PostgresConnectionPool",
    "get_postgres_pool",
    "postgres_cursor",
    "require_psycopg",
    # auth
    "LakebaseAuth",
    "get_lakebase_auth",
    "get_graph_auth",
]
