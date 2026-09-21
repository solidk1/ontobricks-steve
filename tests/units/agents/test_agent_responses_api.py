"""Tests for the chat-completions <-> Responses translation.

``agents.responses_api`` is what lets the engines keep speaking
chat-completions while the wire carries Responses, which is required for
reasoning models: they answer a chat-completions request carrying ``tools``
with HTTP 400, and every engine reads that as "endpoint has no tool support"
and silently drops to tool-less generation.

The round-trip property is what matters. An engine appends the assistant dict
this module produces straight back into its history
(``messages.append(message)``), so :func:`to_chat_response` output must survive
:func:`to_input` on the next turn. Several tests below assert exactly that.

Pure functions, no network.
"""

from __future__ import annotations

from agents import responses_api


# --- messages -> input -------------------------------------------------------


def test_system_and_user_become_input_text():
    items = responses_api.to_input(
        [
            {"role": "system", "content": "You are terse."},
            {"role": "user", "content": "Say OK."},
        ]
    )
    assert items == [
        {"role": "system", "content": [{"type": "input_text", "text": "You are terse."}]},
        {"role": "user", "content": [{"type": "input_text", "text": "Say OK."}]},
    ]


def test_assistant_text_uses_output_text_not_input_text():
    """Assistant turns are model output; the API rejects the wrong part type."""
    items = responses_api.to_input([{"role": "assistant", "content": "hello"}])
    assert items == [
        {"role": "assistant", "content": [{"type": "output_text", "text": "hello"}]}
    ]


def test_assistant_tool_calls_become_function_call_items():
    items = responses_api.to_input(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_x", "arguments": '{"a": 1}'},
                    }
                ],
            }
        ]
    )
    assert items == [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "get_x",
            "arguments": '{"a": 1}',
        }
    ]


def test_tool_result_becomes_function_call_output():
    items = responses_api.to_input(
        [{"role": "tool", "tool_call_id": "call_1", "content": "42"}]
    )
    assert items == [
        {"type": "function_call_output", "call_id": "call_1", "output": "42"}
    ]


def test_assistant_with_text_and_tool_calls_emits_text_first():
    items = responses_api.to_input(
        [
            {
                "role": "assistant",
                "content": "thinking out loud",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "f", "arguments": "{}"}}
                ],
            }
        ]
    )
    assert [i.get("type") or i.get("role") for i in items] == ["assistant", "function_call"]


def test_empty_content_is_dropped():
    """A content list whose only part has no text is rejected by the API."""
    assert responses_api.to_input([{"role": "assistant", "content": ""}]) == []
    assert responses_api.to_input([{"role": "user", "content": None}]) == []


def test_full_tool_loop_history_converts_in_order():
    """The exact shape an engine accumulates over one tool round-trip."""
    items = responses_api.to_input(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
        ]
    )
    assert [i.get("type") or i.get("role") for i in items] == [
        "system",
        "user",
        "function_call",
        "function_call_output",
    ]


# --- tools -------------------------------------------------------------------


def test_nested_tool_schema_is_flattened():
    flat = responses_api.to_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "get_x",
                    "description": "d",
                    "parameters": {"type": "object", "properties": {"a": {"type": "string"}}},
                },
            }
        ]
    )
    assert flat == [
        {
            "type": "function",
            "name": "get_x",
            "description": "d",
            "parameters": {"type": "object", "properties": {"a": {"type": "string"}}},
        }
    ]


def test_already_flat_tool_passes_through():
    already = [{"type": "function", "name": "f", "parameters": {}}]
    assert responses_api.to_tools(already) == already


def test_no_tools_yields_none_not_empty_list():
    assert responses_api.to_tools(None) is None
    assert responses_api.to_tools([]) is None


# --- payload -----------------------------------------------------------------


def test_payload_never_carries_temperature():
    """The Responses API rejects temperature for these models with a 400, which
    is the very failure this module exists to remove."""
    payload = responses_api.build_payload(
        model="databricks-gpt-5-6-sol",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=256,
    )
    assert "temperature" not in payload
    assert payload["model"] == "databricks-gpt-5-6-sol"
    assert payload["max_output_tokens"] == 256
    assert "max_tokens" not in payload
    assert "messages" not in payload


def test_payload_omits_tools_key_when_there_are_none():
    payload = responses_api.build_payload(
        model="m", messages=[{"role": "user", "content": "hi"}]
    )
    assert "tools" not in payload


# --- response -> chat --------------------------------------------------------


def test_message_items_become_content():
    chat = responses_api.to_chat_response(
        {
            "id": "resp_1",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "the answer"}],
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
        }
    )
    msg = chat["choices"][0]["message"]
    assert msg["content"] == "the answer"
    assert "tool_calls" not in msg
    assert chat["choices"][0]["finish_reason"] == "stop"
    # accumulate_usage() reads these two names specifically.
    assert chat["usage"]["prompt_tokens"] == 10
    assert chat["usage"]["completion_tokens"] == 3


def test_function_call_items_become_tool_calls():
    chat = responses_api.to_chat_response(
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_9",
                    "name": "get_x",
                    "arguments": '{"a": 1}',
                }
            ]
        }
    )
    msg = chat["choices"][0]["message"]
    assert msg["tool_calls"] == [
        {
            "id": "call_9",
            "type": "function",
            "function": {"name": "get_x", "arguments": '{"a": 1}'},
        }
    ]
    assert chat["choices"][0]["finish_reason"] == "tool_calls"


def test_reasoning_items_are_dropped():
    """Reasoning is the model's private chain. It cannot be represented in chat
    history, and the live API does not require it echoed back on the next turn."""
    chat = responses_api.to_chat_response(
        {
            "output": [
                {"type": "reasoning", "id": "rs_1", "summary": [{"text": "hmm"}]},
                {"type": "message", "content": [{"type": "output_text", "text": "done"}]},
            ]
        }
    )
    assert chat["choices"][0]["message"]["content"] == "done"


def test_unknown_item_types_are_ignored():
    chat = responses_api.to_chat_response(
        {"output": [{"type": "some_future_item", "payload": {}}]}
    )
    assert chat["choices"][0]["message"]["content"] == ""


def test_empty_output_still_yields_a_parsable_choice():
    """Engines index choices[0] unconditionally."""
    chat = responses_api.to_chat_response({})
    assert chat["choices"][0]["message"] == {"role": "assistant", "content": ""}
    assert chat["usage"]["prompt_tokens"] == 0


# --- round trip --------------------------------------------------------------


def test_converted_reply_survives_being_fed_back_as_history():
    """The engines do messages.append(message) with our output, so the next
    to_input() call must handle it. This is the contract that keeps the tool
    loop working past iteration one."""
    chat = responses_api.to_chat_response(
        {
            "output": [
                {"type": "reasoning", "id": "rs_1"},
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "f",
                    "arguments": "{}",
                },
            ]
        }
    )
    assistant_msg = chat["choices"][0]["message"]

    items = responses_api.to_input(
        [
            {"role": "user", "content": "q"},
            assistant_msg,
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ]
    )
    assert [i.get("type") or i.get("role") for i in items] == [
        "user",
        "function_call",
        "function_call_output",
    ]
    # The call_id must survive, or the model cannot match output to call.
    assert items[1]["call_id"] == "call_1"
    assert items[2]["call_id"] == "call_1"
