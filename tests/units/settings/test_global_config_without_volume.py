"""Global config is readable on a deployment with no Unity Catalog Volume.

Why this file exists
--------------------
``GlobalConfigService.load`` returned an empty dict unless ``registry_cfg``
carried a ``catalog`` *and* a ``schema`` — the UC Volume triplet. But
``_store_for`` ignores the Databricks host and token outright and builds a
Postgres store from the ``PG*`` environment, and ``global_config`` is a
Postgres table (``schema.sql``).

So on a container + Postgres deployment the admin's saved SQL warehouse,
graph-engine config and ``registry_cache_ttl`` were never read. Nothing raised;
``load`` just returned ``_empty()`` and every caller fell through to its
environment-variable default. Three call sites in ``DatabricksHelpers``
repeated the same gate before even calling in.

This is the ``is_configured``/``has_volume`` confusion again, one layer down.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from back.objects.session.GlobalConfigService import GlobalConfigService

pytestmark = pytest.mark.unit

#: A Volume-free registry: Postgres schema only, no catalog/schema/volume.
_NO_VOLUME = {"catalog": "", "schema": "", "volume": "", "postgres_schema": "obreg"}
_WITH_VOLUME = {"catalog": "c", "schema": "s", "volume": "v", "postgres_schema": "obreg"}


class TestRegistryUsablePredicate:
    def test_volume_free_registry_is_usable_with_postgres(
        self, configured_registry_env
    ):
        assert GlobalConfigService._registry_usable(_NO_VOLUME) is True

    def test_not_usable_without_postgres(self):
        """No PGHOST in the suite's environment, so this is the honest answer."""
        assert GlobalConfigService._registry_usable(_NO_VOLUME) is False

    def test_volume_alone_does_not_make_it_usable(self):
        assert GlobalConfigService._registry_usable(_WITH_VOLUME) is False

    def test_never_raises_on_junk(self, configured_registry_env):
        """Called from status payloads, so it must not throw."""
        for junk in (None, {}, {"catalog": None}):
            assert GlobalConfigService._registry_usable(junk) in (True, False)


class TestLoadReachesTheStore:
    def _svc_with_store(self, payload):
        svc = GlobalConfigService()
        store = MagicMock()
        store.load_global_config.return_value = payload
        store.backend = "postgres"
        return svc, store

    def test_load_reads_the_store_without_a_volume(self, configured_registry_env):
        svc, store = self._svc_with_store({"warehouse_id": "wh-saved"})
        with patch.object(GlobalConfigService, "_store_for", return_value=store):
            out = svc.load("", "", _NO_VOLUME, force=True)
        assert out.get("warehouse_id") == "wh-saved"
        store.load_global_config.assert_called_once()

    def test_load_does_not_touch_the_store_without_postgres(self):
        svc, store = self._svc_with_store({"warehouse_id": "wh-saved"})
        with patch.object(GlobalConfigService, "_store_for", return_value=store):
            svc.load("", "", _NO_VOLUME, force=True)
        store.load_global_config.assert_not_called()

    def test_load_needs_no_databricks_host(self, configured_registry_env):
        """``_store_for`` deletes host/token; requiring a host was vestigial."""
        svc, store = self._svc_with_store({"warehouse_id": "wh-saved"})
        with patch.object(GlobalConfigService, "_store_for", return_value=store):
            out = svc.load("", "", _NO_VOLUME, force=True)
        assert out.get("warehouse_id") == "wh-saved"


class TestSaveMessageNamesPostgres:
    """The write path's refusal message must name variables that exist."""

    def _save(self):
        return GlobalConfigService()._save("", "", _NO_VOLUME, {"warehouse_id": "x"})

    def test_unconfigured_save_names_the_postgres_variables(self):
        ok, msg = self._save()
        assert ok is False
        assert "PGHOST" in msg and "ONTOBRICKS_PG_SCHEMA" in msg

    def test_unconfigured_save_does_not_blame_catalog_and_schema(self):
        """It used to say "set catalog and schema in Settings first", which on a
        Volume-free deployment is advice for a thing that does not apply."""
        _, msg = self._save()
        assert "catalog and schema" not in msg
