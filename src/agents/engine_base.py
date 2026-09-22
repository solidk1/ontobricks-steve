"""
Shared infrastructure for OntoBricks agent engines.

Provides the common ``AgentStep`` dataclass and reusable helpers for LLM
serving-endpoint calls, tool dispatch, response content extraction, and
token usage accumulation.  Each concrete agent engine imports what it needs
and focuses exclusively on its own ``AgentResult``, system prompt, and
``run_agent`` loop.
"""

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import requests

from back.core.logging import get_logger
from agents import responses_api
from agents.llm_utils import call_llm_with_retry
from agents.tracing import trace_llm

logger = get_logger(__name__)

# Which OpenAI API to speak. Reasoning models reject function tools on
# chat-completions (see agents/responses_api), and every engine here reads that
# 400 as "no tool support" and falls back to tool-less generation, quietly
# disabling the agent loop. Responses avoids the whole problem, so it is the
# default; set ONTOBRICKS_LLM_API=chat to force the old path for an endpoint
# that only speaks chat-completions.
_LLM_API = os.getenv("ONTOBRICKS_LLM_API", "responses").strip().lower()


def _use_responses_api() -> bool:
    """Read at call time, not import time, so a redeploy's env takes effect."""
    return os.getenv("ONTOBRICKS_LLM_API", _LLM_API).strip().lower() != "chat"

# Endpoints (e.g. databricks-claude-opus-4-7) sometimes reject optional
# OpenAI-style parameters with a 400 message like:
#   "Model ... does not support the temperature parameter."
# We cache such bans per (endpoint, param) pair so subsequent calls skip the
# offending field proactively instead of re-discovering the 400 every time.
_UNSUPPORTED_PARAMS: Dict[str, set] = {}


def _unsupported_params(endpoint_name: str) -> set:
    return _UNSUPPORTED_PARAMS.setdefault(endpoint_name, set())


def _looks_unsupported(body_text: str, param: str) -> bool:
    low = (body_text or "").lower()
    return f"does not support the {param} parameter" in low or (
        "unsupported" in low and param in low
    )


# =====================================================
# Shared data class
# =====================================================


@dataclass
class AgentStep:
    """One observable step of the agent's execution."""

    step_type: str  # tool_call | tool_result | output
    content: str
    tool_name: str = ""
    duration_ms: int = 0


# =====================================================
# LLM call helper
# =====================================================


@trace_llm("agent:llm")
def call_serving_endpoint(
    host: str,
    token: str,
    endpoint_name: str,
    messages: List[dict],
    *,
    tools: Optional[List[dict]] = None,
    max_tokens: int = 2048,
    temperature: float = 0.1,
    timeout: int = 180,
    trace_name: str = "agent:llm",
) -> dict:
    """Call a Databricks serving endpoint (OpenAI-compatible chat completions).

    Builds the URL, headers, and payload, then delegates to
    :func:`call_llm_with_retry` for retry/backoff logic.

    Args:
        trace_name: Used for MLflow span naming via ``@trace_llm``.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    if _use_responses_api():
        return _call_responses_api(
            host,
            headers,
            endpoint_name,
            messages,
            tools=tools,
            max_tokens=max_tokens,
            timeout=timeout,
            trace_name=trace_name,
        )

    # A UC model service has no /serving-endpoints/<name>/invocations route — that
    # returns 404 ENDPOINT_NOT_FOUND — so it goes through the gateway with the name
    # in the body. Only a serving endpoint carries its name in the URL.
    if responses_api.is_model_service(endpoint_name):
        url = f"{host.rstrip('/')}/ai-gateway/mlflow/v1/chat/completions"
    else:
        url = f"{host.rstrip('/')}/serving-endpoints/{endpoint_name}/invocations"

    banned = _unsupported_params(endpoint_name)
    payload: Dict[str, Any] = {
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if responses_api.is_model_service(endpoint_name):
        payload["model"] = endpoint_name
    if "temperature" not in banned and temperature is not None:
        payload["temperature"] = temperature
    if tools:
        payload["tools"] = tools

    logger.info(
        "%s: POST %s — %d messages, %d tool defs, max_tokens=%d, temperature=%s",
        trace_name,
        endpoint_name,
        len(messages),
        len(tools) if tools else 0,
        max_tokens,
        payload.get("temperature", "<skipped>"),
    )

    try:
        resp = call_llm_with_retry(url, headers, payload, timeout=timeout)
        return resp.json()
    except requests.exceptions.HTTPError as exc:
        response = exc.response
        status = response.status_code if response is not None else None
        if status != 400:
            raise
        body_text = response.text if response is not None else ""
        # Detect and strip parameters the model rejects, then retry once.
        dropped: List[str] = []
        for param in ("temperature",):
            if param in payload and _looks_unsupported(body_text, param):
                banned.add(param)
                payload.pop(param, None)
                dropped.append(param)
        if not dropped:
            raise
        logger.warning(
            "%s: endpoint rejected unsupported param(s) %s — retrying without them",
            trace_name,
            dropped,
        )
        resp = call_llm_with_retry(url, headers, payload, timeout=timeout)
        return resp.json()


def _call_responses_api(
    host: str,
    headers: Dict[str, str],
    endpoint_name: str,
    messages: List[dict],
    *,
    tools: Optional[List[dict]] = None,
    max_tokens: int = 2048,
    timeout: int = 180,
    trace_name: str = "agent:llm",
) -> dict:
    """Call the Responses API and return a chat-completions-shaped reply.

    Not separately traced: its only caller, ``call_serving_endpoint``, already
    carries the ``agent:llm`` span, so the span still covers this request.

    Callers are unaware: the reply is translated back into
    ``choices[0].message`` so the engines' existing parsing and their chat-shaped
    ``messages`` history keep working untouched. See ``agents/responses_api``
    for the four shape differences and what was verified on the live endpoint.

    No temperature is sent — the Responses API rejects it for these models — so
    the ``_unsupported_params`` learning this function's chat sibling needs has
    no work to do here.
    """
    url = f"{host.rstrip('/')}{responses_api.responses_path(endpoint_name)}"
    payload = responses_api.build_payload(
        model=endpoint_name,
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
    )

    logger.info(
        "%s: POST %s (responses) — %d input items %s, %d tool defs, max_output_tokens=%d",
        trace_name,
        endpoint_name,
        len(payload.get("input") or []),
        responses_api.describe_payload(payload),
        len(payload.get("tools") or []),
        max_tokens,
    )

    try:
        resp = call_llm_with_retry(url, headers, payload, timeout=timeout)
    except requests.exceptions.HTTPError as exc:
        response = exc.response
        status = response.status_code if response is not None else None
        logger.error(
            "%s: responses API call failed (status=%s): %.500s",
            trace_name,
            status,
            response.text if response is not None else "N/A",
        )
        raise
    return responses_api.to_chat_response(resp.json())


# =====================================================
# Tool dispatch helper
# =====================================================


def dispatch_tool(
    handlers: Dict[str, Callable],
    ctx: Any,
    tool_name: str,
    arguments: dict,
    *,
    trace_name: str = "agent:tool",
) -> str:
    """Dispatch a tool call and return the JSON result string.

    Handles unknown tools and exceptions uniformly across agents.
    """
    handler = handlers.get(tool_name)
    if not handler:
        logger.warning(
            "%s: unknown tool '%s' — available: %s",
            trace_name,
            tool_name,
            list(handlers.keys()),
        )
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
    try:
        t0 = time.time()
        result = handler(ctx, **arguments)
        elapsed = int((time.time() - t0) * 1000)
        logger.info(
            "%s: '%s' completed in %dms, returned %d chars",
            trace_name,
            tool_name,
            elapsed,
            len(result),
        )
        return result
    except Exception as exc:
        logger.exception("%s: '%s' raised exception: %s", trace_name, tool_name, exc)
        return json.dumps({"error": f"Tool execution failed: {exc}"})


# =====================================================
# Response content extraction
# =====================================================


def extract_message_content(llm_response: dict) -> str:
    """Extract text content from an OpenAI-style or predictions-style LLM response."""
    choices = llm_response.get("choices", [])
    if choices:
        content = choices[0].get("message", {}).get("content") or ""
        # Claude endpoints return content as a list of blocks, not a string
        if isinstance(content, list):
            content = "".join(
                b if isinstance(b, str) else b.get("text", "") for b in content
            )
        return content
    preds = llm_response.get("predictions", [])
    if preds:
        return preds[0] if isinstance(preds[0], str) else str(preds[0])
    logger.warning(
        "extract_message_content: no choices or predictions, keys=%s",
        list(llm_response.keys()),
    )
    return ""


# =====================================================
# Token usage accumulation
# =====================================================


def accumulate_usage(total: Dict[str, int], usage_block: dict) -> None:
    """Add prompt/completion token counts from *usage_block* into *total* in-place."""
    total["prompt_tokens"] = total.get("prompt_tokens", 0) + usage_block.get(
        "prompt_tokens", 0
    )
    total["completion_tokens"] = total.get("completion_tokens", 0) + usage_block.get(
        "completion_tokens", 0
    )
