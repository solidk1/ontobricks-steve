"""A resolved :class:`LLMTarget` for tests.

Agents take a target rather than a Databricks ``(host, token, endpoint_name)``
triple, so tests construct one directly instead of relying on environment
variables. Building it explicitly also keeps the unit suite honest: nothing here
reads ``ONTOBRICKS_LLM_*``, so a test cannot accidentally pass because the
developer's shell happened to be configured.
"""

from __future__ import annotations

from shared.config.LLMTarget import LLMTarget

TEST_BASE_URL = "https://llm.test/v1"


def llm_target(model: str = "ep", *, api_key: str = "test-key") -> LLMTarget:
    """Return a target whose model is *model*."""
    return LLMTarget(base_url=TEST_BASE_URL, api_key=api_key, model=model)
