"""The Unity Catalog Volume is optional; the structured registry is not.

Why this file exists
--------------------
``RegistryCfg.is_configured`` used to mean "the UC Volume triplet is set".
Every one of ~37 call sites read it as "the registry is usable", which was
true only as long as the Volume and the Postgres registry were bound
together by the Databricks Apps deploy.

Once OntoBricks could deploy to a plain container plus a plain Postgres,
the two came apart, and a deployment with no Volume at all reported
*"Registry is not operational -- not configured"* while its Postgres
registry was up and reachable. The whole Python suite stayed green,
because no test ever constructed a config with Postgres but no Volume.

That is the gap these tests close: the truth table for the two properties,
and the one call site (``uc_domain_path``) that genuinely does need the
Volume rather than the registry.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from back.objects.registry.RegistryService import RegistryCfg

pytestmark = pytest.mark.unit


def _with_postgres(available: bool):
    """Patch ``PostgresAuth`` as seen by ``is_configured``'s local import."""
    return patch(
        "back.core.postgres.PostgresAuth.PostgresAuth",
        return_value=MagicMock(is_available=available),
    )


class TestTruthTable:
    """``is_configured`` is a Postgres question; ``has_volume`` is a UC one."""

    def test_postgres_only_deployment_is_configured(self):
        cfg = RegistryCfg(catalog="", schema="", volume="")
        with _with_postgres(True):
            assert cfg.is_configured is True

    def test_postgres_only_deployment_has_no_volume(self):
        cfg = RegistryCfg(catalog="", schema="", volume="")
        assert cfg.has_volume is False

    def test_volume_without_postgres_is_not_configured(self):
        cfg = RegistryCfg(catalog="cat", schema="sch", volume="vol")
        with _with_postgres(False):
            assert cfg.is_configured is False

    def test_volume_without_postgres_still_has_volume(self):
        cfg = RegistryCfg(catalog="cat", schema="sch", volume="vol")
        assert cfg.has_volume is True

    def test_partial_volume_triplet_is_not_a_volume(self):
        cfg = RegistryCfg(catalog="cat", schema="sch", volume="")
        assert cfg.has_volume is False

    def test_blank_postgres_schema_is_not_configured(self):
        """No schema name means nothing to address, whatever the server says."""
        cfg = RegistryCfg(catalog="", schema="", volume="", postgres_schema="")
        with _with_postgres(True):
            assert cfg.is_configured is False

    def test_unreachable_postgres_does_not_raise(self):
        """``is_configured`` is called from status payloads; it must not throw."""
        cfg = RegistryCfg(catalog="", schema="", volume="")
        with patch(
            "back.core.postgres.PostgresAuth.PostgresAuth",
            side_effect=RuntimeError("no server"),
        ):
            assert cfg.is_configured is False


class TestStatusPayload:
    """The UI branches on both flags, so ``as_dict`` must carry both."""

    def test_as_dict_exposes_has_volume(self):
        assert "has_volume" in RegistryCfg(catalog="", schema="", volume="").as_dict()

    def test_as_dict_has_volume_tracks_the_triplet(self):
        assert RegistryCfg("c", "s", "v").as_dict()["has_volume"] is True
        assert RegistryCfg("", "", "").as_dict()["has_volume"] is False


class TestVolumePathGuard:
    """``uc_domain_path`` builds a ``/Volumes/`` string, so it needs the Volume.

    This is the only site that switched from ``is_configured`` to
    ``has_volume``. The negative control matters more than the positive
    one: without the Volume it must return the empty string rather than
    ``/Volumes///domains/...``.
    """

    def _session(self, cfg: RegistryCfg):
        """Exercise the real property with only its two inputs faked.

        ``get_settings`` is imported *inside* the property, so it is patched
        at its source module, not on DomainSession.
        """
        from back.objects.session.DomainSession import DomainSession

        sess = DomainSession.__new__(DomainSession)
        with patch.object(
            RegistryCfg, "from_domain", classmethod(lambda cls, *a, **k: cfg)
        ), patch.object(
            DomainSession, "uc_domain_folder", property(lambda self: "demo")
        ), patch(
            "shared.config.settings.get_settings", MagicMock()
        ):
            return sess.uc_domain_path

    def test_returns_empty_string_without_a_volume(self):
        assert self._session(RegistryCfg("", "", "")) == ""

    def test_builds_a_volumes_path_when_the_triplet_is_set(self):
        assert self._session(RegistryCfg("cat", "sch", "vol")).startswith(
            "/Volumes/cat/sch/vol/"
        )

    def test_never_emits_a_path_with_empty_segments(self):
        """The pre-split bug shape: a truthy guard producing /Volumes///..."""
        assert "//" not in self._session(RegistryCfg("cat", "sch", "vol")).lstrip("/")
