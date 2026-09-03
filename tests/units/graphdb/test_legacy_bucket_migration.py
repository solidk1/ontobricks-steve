"""A stored config written before 0.8 must keep resolving.

Two values were persisted with the name ``lakebase``:

* ``global_config.graph_engine_config.lakebase`` — the connection/options bucket.
* ``domain.info.graph_backend`` — the per-domain engine selection, in the domain
  JSON document.

Both are now canonically ``postgres``. Nothing writes the old name any more, but
existing rows and documents still carry it, so read must accept either. These
tests are the migration contract.
"""

from __future__ import annotations

import pytest

from back.core.graphdb.engine_config import (
    LEGACY_PG_BUCKET,
    PG_BUCKET,
    normalize_graph_engine_config,
    postgres_section,
)
from back.core.graphdb.GraphDBFactory import (
    DEFAULT_GRAPH_BACKEND,
    GRAPH_BACKENDS,
    normalize_graph_backend,
)

pytestmark = pytest.mark.unit


class TestCanonicalNames:
    def test_bucket_is_postgres(self):
        assert PG_BUCKET == "postgres"
        assert LEGACY_PG_BUCKET == "lakebase"

    def test_graph_backends_use_postgres(self):
        assert "postgres" in GRAPH_BACKENDS
        assert "lakebase" not in GRAPH_BACKENDS
        assert DEFAULT_GRAPH_BACKEND == "postgres"


class TestBucketRead:
    def test_legacy_bucket_resolves(self):
        stored = {"lakebase": {"schema": "g", "database": "d"}, "neo4j": {}}
        assert postgres_section(stored) == {"schema": "g", "database": "d"}

    def test_canonical_bucket_resolves(self):
        stored = {"postgres": {"schema": "g"}, "neo4j": {}}
        assert postgres_section(stored) == {"schema": "g"}

    def test_canonical_wins_where_both_exist(self):
        """`postgres` is what is written today, so it is the fresher value."""
        stored = {"lakebase": {"schema": "old", "database": "keep"},
                  "postgres": {"schema": "new"}}
        section = postgres_section(stored)
        assert section["schema"] == "new"
        assert section["database"] == "keep", "legacy-only keys must survive the merge"

    def test_normalize_always_emits_the_canonical_key(self):
        for stored in ({"lakebase": {"schema": "g"}}, {"postgres": {"schema": "g"}}):
            out = normalize_graph_engine_config(stored)
            assert PG_BUCKET in out
            assert LEGACY_PG_BUCKET not in out

    def test_flat_legacy_shape_still_folds_into_postgres(self):
        """Pre-nesting configs were a flat blob with no bucket at all."""
        assert postgres_section({"schema": "flat", "database": "d"}) == {
            "schema": "flat", "database": "d",
        }

    def test_neo4j_bucket_is_not_absorbed(self):
        stored = {"lakebase": {"schema": "g"}, "neo4j": {"connections": []}}
        out = normalize_graph_engine_config(stored)
        assert out["neo4j"] == {"connections": []}
        assert out[PG_BUCKET] == {"schema": "g"}

    def test_empty_config_yields_all_three_buckets(self):
        assert sorted(normalize_graph_engine_config(None)) == [
            "lakehouse", "neo4j", "postgres",
        ]


class TestGraphBackendRead:
    def test_legacy_value_maps_to_postgres(self):
        assert normalize_graph_backend("lakebase") == "postgres"

    def test_case_and_whitespace_tolerated(self):
        assert normalize_graph_backend("  LakeBase ") == "postgres"

    def test_canonical_value_passes_through(self):
        assert normalize_graph_backend("postgres") == "postgres"

    @pytest.mark.parametrize("value", ["neo4j", "databricks"])
    def test_other_backends_unaffected(self, value):
        assert normalize_graph_backend(value) == value

    @pytest.mark.parametrize("value", ["", None, "wat"])
    def test_unknown_falls_back_to_default(self, value):
        assert normalize_graph_backend(value) == DEFAULT_GRAPH_BACKEND
