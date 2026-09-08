"""Login must not hinge on a SCIM round-trip.

A live deployment failed sign-in with "Could not resolve the signed-in user via
SCIM /Me" even though the access token already carried the identity: Databricks
issues a JWT whose ``sub`` is the user's e-mail. The e-mail now comes from that
claim, and SCIM ``/Me`` is enrichment only (display name, groups), so a SCIM
failure degrades instead of blocking the login.

Groups matter for group-based app-role grants, so losing them narrows access to
direct grants -- which is a degradation, not a bypass.
"""

import base64
import json

import pytest

from back.core.errors import InfrastructureError
from back.objects.identity.OIDCClient import OIDCClient

pytestmark = pytest.mark.unit


def _jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.signature"


class TestEmailFromTokenClaims:
    def test_reads_sub_as_the_email(self):
        tok = _jwt({"sub": "steve.shao@databricks.com"})
        assert OIDCClient._email_from_token(tok) == "steve.shao@databricks.com"

    @pytest.mark.parametrize(
        "token,why",
        [
            (_jwt({"sub": "1234-client-id"}), "service principal sub is not an email"),
            (_jwt({"aud": ["x"]}), "no sub claim"),
            ("dapi0123456789abcdef", "opaque token, not a JWT"),
            ("a.!!!not-base64!!!.c", "malformed payload"),
            ("", "empty"),
        ],
    )
    def test_degrades_to_empty_rather_than_raising(self, token, why):
        """An unusable token must fall through to SCIM, not crash the callback."""
        assert OIDCClient._email_from_token(token) == "", why

    def test_does_not_verify_the_signature(self):
        """Documented deliberate choice: provenance is the code exchange itself.

        The token arrives directly from the Databricks token endpoint over TLS in
        exchange for our client secret plus the PKCE verifier, so it is never a
        caller-supplied bearer token. A garbage signature must still parse.
        """
        tok = _jwt({"sub": "u@example.com"}).rsplit(".", 1)[0] + ".not-a-real-signature"
        assert OIDCClient._email_from_token(tok) == "u@example.com"


class TestScimIsEnrichmentOnly:
    def test_scim_failure_does_not_block_login(self, monkeypatch):
        import requests

        def boom(*a, **k):
            raise requests.exceptions.ConnectTimeout("simulated SCIM outage")

        monkeypatch.setattr(requests, "get", boom)
        monkeypatch.setenv("DATABRICKS_HOST", "https://example.azuredatabricks.net")
        out = OIDCClient().fetch_identity(_jwt({"sub": "u@example.com"}))
        assert out["email"] == "u@example.com"
        assert out["groups"] == [], "no groups without SCIM, so direct grants only"

    def test_raises_only_when_both_sources_fail(self, monkeypatch):
        import requests

        def boom(*a, **k):
            raise requests.exceptions.ConnectTimeout("simulated SCIM outage")

        monkeypatch.setattr(requests, "get", boom)
        monkeypatch.setenv("DATABRICKS_HOST", "https://example.azuredatabricks.net")
        with pytest.raises(InfrastructureError) as exc:
            OIDCClient().fetch_identity("opaque-not-a-jwt")
        assert "SCIM" in str(exc.value)

    def test_scim_still_supplies_groups_when_it_works(self, monkeypatch):
        import requests

        class Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "userName": "ignored@example.com",
                    "displayName": "Steve Shao",
                    "groups": [{"display": "data-eng"}, {"display": "admins"}],
                }

        monkeypatch.setattr(requests, "get", lambda *a, **k: Resp())
        monkeypatch.setenv("DATABRICKS_HOST", "https://example.azuredatabricks.net")
        out = OIDCClient().fetch_identity(_jwt({"sub": "u@example.com"}))
        assert out["email"] == "u@example.com", "token claim wins over SCIM userName"
        assert out["display_name"] == "Steve Shao"
        assert out["groups"] == ["data-eng", "admins"]
