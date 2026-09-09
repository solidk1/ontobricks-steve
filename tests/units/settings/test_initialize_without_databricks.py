"""Initialize works on a PostgreSQL-only deployment.

Why this file exists
--------------------
The Registry panel said *"connected to PostgreSQL but schema … is not
initialized yet. Click **Initialize**"* and clicking it failed with
**"Databricks not configured"**.

``initialize_registry_result`` demanded a Databricks client unconditionally, even
though the only thing it is used for is creating the optional Unity Catalog
Volume — and ``RegistryService.initialize`` already skips that step when the
client is ``None`` and the triplet is unset. So the requirement was invented one
layer above the code that knew better, and it made the registry impossible to
create off Databricks: the panel told the user to click a button that could not
succeed.

The client is required only when a Volume *is* configured, since then there is
genuinely something to create.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from back.core.errors import ValidationError
from back.objects.domain.SettingsService import SettingsService

pytestmark = pytest.mark.unit


def _svc(*, has_volume: bool, configured: bool = True):
    svc = MagicMock()
    svc.cfg.is_configured = configured
    svc.cfg.has_volume = has_volume
    svc.cfg.catalog, svc.cfg.schema, svc.cfg.volume = ("c", "s", "v") if has_volume else ("", "", "")
    svc.initialize.return_value = (True, "initialised")
    return svc


def _run(svc, client):
    with patch("back.objects.domain.SettingsService.get_domain"), patch(
        "back.objects.domain.SettingsService.RegistryService"
    ) as rs, patch(
        "back.objects.domain.SettingsService.get_databricks_client",
        return_value=client,
    ):
        rs.from_context.return_value = svc
        return SettingsService.initialize_registry_result(MagicMock(), MagicMock())


class TestPostgresOnly:
    def test_initialize_succeeds_with_no_databricks_client(self):
        svc = _svc(has_volume=False)
        out = _run(svc, None)
        assert out.get("success") is not False
        svc.initialize.assert_called_once()

    def test_the_none_client_is_handed_to_the_service(self):
        """RegistryService.initialize is the layer that knows the Volume step is
        optional, so the decision belongs there, not in a pre-check."""
        svc = _svc(has_volume=False)
        _run(svc, None)
        assert svc.initialize.call_args.args[0] is None

    def test_a_client_is_still_used_when_available(self):
        svc, client = _svc(has_volume=False), MagicMock()
        _run(svc, client)
        assert svc.initialize.call_args.args[0] is client


class TestVolumeConfiguredButNoCredentials:
    def test_it_refuses_because_there_is_something_to_create(self):
        svc = _svc(has_volume=True)
        with pytest.raises(ValidationError) as exc:
            _run(svc, None)
        assert "Volume" in str(exc.value)

    def test_the_refusal_names_the_volume_and_the_ways_out(self):
        svc = _svc(has_volume=True)
        with pytest.raises(ValidationError) as exc:
            _run(svc, None)
        msg = str(exc.value)
        assert "c.s.v" in msg
        assert "service principal" in msg and "clear the Volume" in msg


class TestUnreachableRegistry:
    def test_message_names_the_postgres_variables(self):
        svc = _svc(has_volume=False, configured=False)
        with pytest.raises(ValidationError) as exc:
            _run(svc, None)
        msg = str(exc.value)
        assert "PGHOST" in msg and "ONTOBRICKS_PG_SCHEMA" in msg

    def test_message_does_not_demand_catalog_and_volume(self):
        """It used to say "catalog, schema, and volume must be configured
        first", which is not what is_configured checks any more."""
        svc = _svc(has_volume=False, configured=False)
        with pytest.raises(ValidationError) as exc:
            _run(svc, None)
        assert "volume must be configured" not in str(exc.value)
