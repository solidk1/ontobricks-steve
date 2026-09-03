"""Caller identity resolution."""

from back.objects.identity.IdentityResolver import (  # noqa: F401
    SESSION_EMAIL,
    SESSION_GROUPS,
    SESSION_NAME,
    SESSION_TOKEN,
    Identity,
    IdentityResolver,
    identity_of,
)

__all__ = [
    "Identity",
    "IdentityResolver",
    "identity_of",
    "SESSION_EMAIL",
    "SESSION_NAME",
    "SESSION_TOKEN",
    "SESSION_GROUPS",
]
