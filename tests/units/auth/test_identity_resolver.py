"""Tests for IdentityResolver — the single source of caller identity.

25 call sites used to read ``x-forwarded-*`` directly, which meant a container
deployment (no Apps proxy) had no identity at all. The resolution order is the
substance here: session first so OIDC wins, proxy headers second so an existing
Apps deployment keeps working, SCIM last so local development still attributes
audit records to a real developer.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from back.objects.identity import Identity, IdentityResolver

pytestmark = pytest.mark.unit


def _request(session=None, headers=None):
    """Build a request-like object.

    Session data goes on ``request.state.session``, which is where OntoBricks'
    own FileSessionMiddleware puts it — Starlette's ``request.session``
    property asserts when SessionMiddleware is not installed.
    """
    req = SimpleNamespace(state=SimpleNamespace())
    if session is not None:
        req.state.session = session
    if headers is not None:
        req.headers = headers
    return req


class _RaisingSessionRequest:
    """Mimics a Starlette Request with no SessionMiddleware installed."""

    def __init__(self, headers):
        self.headers = headers
        self.state = SimpleNamespace()

    @property
    def session(self):
        raise AssertionError("SessionMiddleware must be installed")


class TestIdentity:
    def test_empty_identity_is_not_authenticated(self):
        assert Identity().is_authenticated is False

    def test_email_alone_authenticates(self):
        assert Identity(email="a@b.c").is_authenticated is True

    def test_label_prefers_display_name(self):
        assert Identity(email="a@b.c", display_name="Ada").label == "Ada"

    def test_label_falls_back_to_email(self):
        assert Identity(email="a@b.c").label == "a@b.c"

    def test_access_token_may_be_absent(self):
        """Callers must cope: not every auth path yields a Databricks token."""
        assert Identity(email="a@b.c").access_token == ""


class TestSessionSource:
    def test_reads_oidc_session(self):
        req = _request(
            session={
                "auth_email": "ada@example.com",
                "auth_display_name": "Ada L",
                "auth_access_token": "tok",
                "auth_groups": ["admins", "eng"],
            }
        )
        ident = IdentityResolver.resolve(req)
        assert ident.email == "ada@example.com"
        assert ident.display_name == "Ada L"
        assert ident.access_token == "tok"
        assert ident.groups == ["admins", "eng"]
        assert ident.source == "session"

    def test_session_without_email_is_not_an_identity(self):
        assert (
            IdentityResolver.from_session(_request(session={"auth_groups": ["x"]}))
            is None
        )

    def test_missing_session_attribute_is_tolerated(self):
        assert IdentityResolver.from_session(_request(headers={})) is None

    def test_raising_session_property_is_tolerated(self):
        """Starlette asserts on request.session without SessionMiddleware.

        Guarding only the lookup and not the attribute access broke every
        request in the suite, so this pins the access itself.
        """
        req = _RaisingSessionRequest({"x-forwarded-email": "ada@example.com"})
        assert IdentityResolver.from_session(req) is None
        assert IdentityResolver.resolve(req).email == "ada@example.com"

    def test_session_wins_over_headers(self):
        """OIDC must take precedence, so a proxy cannot spoof past a login."""
        req = _request(
            session={"auth_email": "real@example.com"},
            headers={"x-forwarded-email": "spoof@example.com"},
        )
        assert IdentityResolver.resolve(req).email == "real@example.com"


class TestHeaderSource:
    def test_reads_proxy_headers(self):
        req = _request(
            headers={
                "x-forwarded-email": "ada@example.com",
                "x-forwarded-preferred-username": "ada",
                "x-forwarded-access-token": "tok",
            }
        )
        ident = IdentityResolver.resolve(req)
        assert ident.email == "ada@example.com"
        assert ident.display_name == "ada"
        assert ident.access_token == "tok"
        assert ident.source == "proxy_headers"

    def test_username_alone_is_enough(self):
        req = _request(headers={"x-forwarded-preferred-username": "ada"})
        assert IdentityResolver.resolve(req).email == "ada"

    def test_no_identity_headers_returns_none(self):
        assert (
            IdentityResolver.from_headers(_request(headers={"accept": "*/*"})) is None
        )


class TestLocalFallback:
    def test_used_when_nothing_else_resolves(self, monkeypatch):
        monkeypatch.setattr(
            IdentityResolver,
            "from_local_workspace",
            staticmethod(lambda: Identity(email="dev@local", source="local_workspace")),
        )
        ident = IdentityResolver.resolve(_request(headers={}))
        assert ident.email == "dev@local"
        assert ident.source == "local_workspace"

    def test_suppressed_when_auth_is_enforced(self, monkeypatch):
        """The important one.

        Falling back to the deploying principal's SCIM identity while auth is
        enforced would authenticate every anonymous request as that user.
        """
        monkeypatch.setattr(
            IdentityResolver,
            "from_local_workspace",
            staticmethod(lambda: Identity(email="dev@local")),
        )
        ident = IdentityResolver.resolve(_request(headers={}), allow_local=False)
        assert ident.is_authenticated is False
