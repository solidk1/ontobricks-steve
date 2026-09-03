"""Tests for EntraCredential — token-as-password for Azure Postgres.

The cadence matters more than the mechanics: Entra tokens live 5-60 minutes and
cannot be refreshed inside an open session, so a token must be minted per new
*physical* connection and dropped the moment authentication fails.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from back.core.errors import InfrastructureError
from back.core.postgres.EntraCredential import (
    OSSRDBMS_SCOPE,
    REFRESH_MARGIN_S,
    EntraCredential,
)

pytestmark = pytest.mark.unit


class _FakeCredential:
    """Records scopes requested and hands out sequential tokens."""

    def __init__(self, ttl: float = 3600.0, fail: bool = False, token: str = ""):
        self.calls: list[str] = []
        self._ttl = ttl
        self._fail = fail
        self._forced = token
        self._n = 0

    def get_token(self, scope):
        self.calls.append(scope)
        if self._fail:
            raise RuntimeError("no managed identity available")
        self._n += 1
        return SimpleNamespace(
            token=self._forced if self._forced != "" else f"tok-{self._n}",
            expires_on=time.time() + self._ttl,
        )


class TestScope:
    def test_requests_the_ossrdbms_audience(self):
        fake = _FakeCredential()
        EntraCredential(credential=fake).token()
        assert fake.calls == [OSSRDBMS_SCOPE]

    def test_scope_value_is_the_documented_one(self):
        assert OSSRDBMS_SCOPE == "https://ossrdbms-aad.database.windows.net/.default"


class TestCaching:
    def test_reuses_a_valid_token(self):
        fake = _FakeCredential()
        cred = EntraCredential(credential=fake)
        assert cred.token() == "tok-1"
        assert cred.token() == "tok-1"
        assert len(fake.calls) == 1

    def test_remints_before_expiry_margin(self):
        """A token expiring within the margin must not be handed out.

        Otherwise it could expire mid-handshake and fail the connection.
        """
        fake = _FakeCredential(ttl=REFRESH_MARGIN_S - 1)
        cred = EntraCredential(credential=fake)
        assert cred.token() == "tok-1"
        assert cred.token() == "tok-2"
        assert len(fake.calls) == 2

    def test_invalidate_forces_a_fresh_token(self):
        """The pool calls invalidate() on auth failure; that must re-mint."""
        fake = _FakeCredential()
        cred = EntraCredential(credential=fake)
        assert cred.token() == "tok-1"
        cred.invalidate()
        assert cred.token() == "tok-2"


class TestFailures:
    def test_credential_error_becomes_infrastructure_error(self):
        cred = EntraCredential(credential=_FakeCredential(fail=True))
        with pytest.raises(InfrastructureError) as exc:
            cred.token()
        msg = str(exc.value)
        assert "managed identity" in msg and "az login" in msg

    def test_whitespace_only_token_is_rejected(self):
        """Truthy but useless: libpq would fail with an opaque auth error."""
        cred = EntraCredential(credential=_FakeCredential(token="   "))
        with pytest.raises(InfrastructureError, match="empty token"):
            cred.token()

    def test_missing_token_attribute_is_rejected(self):
        class _NoToken:
            def get_token(self, scope):
                return SimpleNamespace(expires_on=time.time() + 600)

        with pytest.raises(InfrastructureError, match="empty token"):
            EntraCredential(credential=_NoToken()).token()
