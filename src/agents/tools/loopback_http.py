"""
Shared loopback HTTP helpers for agent tool modules.

Both ``agent_dtwin_chat/tools.py`` and ``agent_cohort/tools.py`` issue
synchronous HTTP requests over loopback to the running OntoBricks app.
This module centralises the common plumbing so each agent only has to
supply its name and, optionally, a timeout.
"""

from __future__ import annotations

import httpx

from agents.tools.context import ToolContext
from shared.config.constants import HTTP_USER_AGENT

_DEFAULT_TIMEOUT = 60


def loopback_client(
    ctx: ToolContext, *, timeout: int = _DEFAULT_TIMEOUT
) -> httpx.Client:
    """Build a sync httpx.Client bound to the loopback OntoBricks URL.

    Session cookies AND the user's Databricks-Apps ``X-Forwarded-*``
    identity headers are forwarded so the loopback route resolves the
    same active session *and* passes the ``PermissionMiddleware`` on
    the deployed app.
    """
    return httpx.Client(
        base_url=ctx.dtwin_base_url or "http://localhost:8000",
        cookies=ctx.dtwin_session_cookies or {},
        headers={"User-Agent": HTTP_USER_AGENT, **(ctx.dtwin_session_headers or {})},
        timeout=timeout,
        follow_redirects=False,
    )


def loopback_registry_params(ctx: ToolContext) -> dict:
    """Build the registry query-parameter dict from the tool context."""
    return {k: v for k, v in (ctx.dtwin_registry_params or {}).items() if v}
