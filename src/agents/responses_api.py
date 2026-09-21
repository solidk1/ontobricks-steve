"""Translate between OpenAI chat-completions and OpenAI Responses shapes.

Why this exists
---------------
Reasoning models served by Databricks refuse function tools on
``/v1/chat/completions``. ``databricks-gpt-5-6-sol`` answers a request that
carries ``tools`` with HTTP 400::

    Function tools with reasoning_effort are not supported for gpt-5.6-sol in
    /v1/chat/completions. To use function tools, use /v1/responses or set
    reasoning_effort to 'none'.

Every agent engine here treats that 400 as "endpoint cannot do tools" and
retries with ``tools=None`` ("using direct generation…"), which silently drops
the agent loop: no tool calls, no iterative refinement, one-shot output. The
other way out of the 400 is ``reasoning_effort: "none"``, which trades away the
reasoning these models are chosen for. So the fix is to speak Responses.

The engines are unchanged. They keep chat-shaped history — a system/user dict,
the assistant dict echoed straight back via ``messages.append(message)``, then
``{"role": "tool", "tool_call_id": …}`` — and they read
``choices[0].message.tool_calls``. This module converts on the way out and
converts the reply back, so the translation stays in one place instead of
spreading across four engines.

Four differences it absorbs:

======================  ==============================  =============================
                        chat completions                 responses
======================  ==============================  =============================
endpoint name           in the URL path                  ``model`` in the body
conversation            ``messages``                     ``input``, typed content parts
tool schema             nested under ``function``        flat ``{type, name, …}``
reply                   ``choices[0].message``           ``output[]`` item list
======================  ==============================  =============================

Verified against ``databricks-gpt-5-6-sol`` on
``/ai-gateway/mlflow/v1/responses``:

* tools are accepted and ``function_call`` items come back, reasoning left on
* a multi-turn ``function_call`` + ``function_call_output`` sequence resolves
  correctly **without** echoing the model's ``reasoning`` items back, so
  chat-shaped history (which cannot represent them) loses nothing
* ``temperature`` is rejected outright — see ``build_payload``
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

# The Responses surface is not under /serving-endpoints/<name>/invocations —
# that path is chat-only and answers an `input` body with
# "Missing required Chat parameter: 'messages'". The endpoint name moves into
# the payload's `model` field.
RESPONSES_PATH = "/ai-gateway/mlflow/v1/responses"

_TEXT_PART_TYPES = ("input_text", "output_text", "text")


def _content_to_text(content: Any) -> str:
    """Flatten a chat message's ``content`` to plain text.

    Engines here always set a string, but an assistant dict echoed back from
    :func:`to_chat_response` may carry ``None`` when the turn was tool calls
    only, and a caller could reasonably pass content parts.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in _TEXT_PART_TYPES:
                parts.append(part.get("text") or "")
        return "".join(parts)
    return str(content)


def to_input(messages: Iterable[dict]) -> List[dict]:
    """Convert chat ``messages`` into Responses ``input`` items.

    Roles map to typed content parts, and the two tool-related shapes become
    standalone items rather than messages:

    * ``assistant`` carrying ``tool_calls`` -> one ``function_call`` per call
    * ``{"role": "tool", "tool_call_id": …}`` -> ``function_call_output``

    An assistant turn with both text and tool calls yields the text item first,
    matching the order the model produced them.
    """
    items: List[dict] = []
    for msg in messages:
        role = msg.get("role")

        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id") or "",
                    "output": _content_to_text(msg.get("content")),
                }
            )
            continue

        if role == "assistant":
            text = _content_to_text(msg.get("content"))
            if text:
                items.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }
                )
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.get("id") or "",
                        "name": fn.get("name") or "",
                        # Arguments travel as a JSON *string* in both APIs.
                        "arguments": fn.get("arguments") or "{}",
                    }
                )
            continue

        # system / developer / user. An empty string is dropped: the API rejects
        # a content list whose only part has no text.
        text = _content_to_text(msg.get("content"))
        if text:
            items.append(
                {
                    "role": role or "user",
                    "content": [{"type": "input_text", "text": text}],
                }
            )
    return items


def to_tools(tools: Optional[Iterable[dict]]) -> Optional[List[dict]]:
    """Flatten chat tool definitions into Responses tool definitions.

    Chat nests the schema under ``function``; Responses hoists it to the top
    level. A definition that is already flat passes through unchanged, so a
    caller that has been updated does not get mangled.
    """
    if not tools:
        return None
    flat: List[dict] = []
    for tool in tools:
        fn = tool.get("function")
        if not isinstance(fn, dict):
            flat.append(tool)  # already flat
            continue
        flat.append(
            {
                "type": "function",
                "name": fn.get("name") or "",
                "description": fn.get("description") or "",
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return flat


def build_payload(
    *,
    model: str,
    messages: Iterable[dict],
    tools: Optional[Iterable[dict]] = None,
    max_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    """Assemble a Responses request body.

    ``temperature`` is deliberately absent and takes no parameter. The Responses
    API supports a narrower set of inference parameters, and the reasoning models
    this exists for reject it outright::

        Unsupported parameter: 'temperature' is not supported with this model.

    Passing it through would turn every call into the 400 this module was written
    to eliminate. ``max_tokens`` becomes ``max_output_tokens``.
    """
    payload: Dict[str, Any] = {
        "model": model,
        "input": to_input(messages),
    }
    if max_tokens is not None:
        payload["max_output_tokens"] = max_tokens
    flat_tools = to_tools(tools)
    if flat_tools:
        payload["tools"] = flat_tools
    return payload


def to_chat_response(response: dict) -> Dict[str, Any]:
    """Convert a Responses reply into the chat-completions shape.

    Callers keep reading ``choices[0].message`` / ``.tool_calls`` and
    ``usage.prompt_tokens``, so the ``output[]`` item list is collapsed back:
    ``message`` items concatenate into ``content``, ``function_call`` items
    become ``tool_calls``, and ``reasoning`` items are dropped — they are the
    model's private chain, they cannot be represented in chat history, and the
    API does not require them echoed back.
    """
    content_parts: List[str] = []
    tool_calls: List[dict] = []

    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "{}",
                    },
                }
            )
        elif kind == "message":
            content_parts.append(_content_to_text(item.get("content")))
        # `reasoning` and anything unrecognised: intentionally ignored.

    message: Dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts),
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    usage_in = response.get("usage") or {}
    usage = {
        # accumulate_usage() reads prompt_tokens / completion_tokens.
        "prompt_tokens": usage_in.get("input_tokens", 0),
        "completion_tokens": usage_in.get("output_tokens", 0),
        "total_tokens": usage_in.get("total_tokens", 0),
    }

    return {
        "id": response.get("id", ""),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": usage,
    }


def describe_payload(payload: dict) -> str:
    """One-line summary for logs — item and tool counts, never message text."""
    items = payload.get("input") or []
    kinds: Dict[str, int] = {}
    for item in items:
        kind = item.get("type") or item.get("role") or "?"
        kinds[kind] = kinds.get(kind, 0) + 1
    return json.dumps(kinds, sort_keys=True)
