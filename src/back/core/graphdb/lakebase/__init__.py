"""Lakebase Postgres implementation of the OntoBricks graph DB engine."""

from back.core.graphdb.lakebase.LakebaseBase import (
    DEFAULT_GRAPH_SCHEMA,
    default_schema,
    validate_engine_config_keys,
    validate_graph_schema,
)

try:
    from back.core.graphdb.lakebase.pool import _require_psycopg

    _require_psycopg()
    from back.core.graphdb.lakebase.LakebaseFlatStore import LakebaseFlatStore  # noqa: F401

    LAKEBASE_AVAILABLE = True
except ImportError:  # pragma: no cover
    LAKEBASE_AVAILABLE = False
    LakebaseFlatStore = None  # type: ignore[misc, assignment]

__all__ = [
    "DEFAULT_GRAPH_SCHEMA",
    "LAKEBASE_AVAILABLE",
    "LakebaseFlatStore",
    "default_schema",
    "validate_engine_config_keys",
    "validate_graph_schema",
]

