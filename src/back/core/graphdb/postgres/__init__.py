"""Lakebase Postgres implementation of the OntoBricks graph DB engine."""

from back.core.graphdb.postgres.PostgresBase import (
    DEFAULT_GRAPH_SCHEMA,
    default_schema,
    validate_engine_config_keys,
    validate_graph_schema,
)

try:
    from back.core.graphdb.postgres.pool import _require_psycopg

    _require_psycopg()
    from back.core.graphdb.postgres.PostgresFlatStore import PostgresFlatStore  # noqa: F401

    POSTGRES_AVAILABLE = True
except ImportError:  # pragma: no cover
    POSTGRES_AVAILABLE = False
    PostgresFlatStore = None  # type: ignore[misc, assignment]

__all__ = [
    "DEFAULT_GRAPH_SCHEMA",
    "POSTGRES_AVAILABLE",
    "PostgresFlatStore",
    "default_schema",
    "validate_engine_config_keys",
    "validate_graph_schema",
]

