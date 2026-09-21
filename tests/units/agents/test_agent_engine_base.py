"""Tests for agents.engine_base – shared agent infrastructure."""

import json
import pytest
from unittest.mock import patch, MagicMock
from dataclasses import asdict

from agents.engine_base import (
    AgentStep,
    call_serving_endpoint,
    dispatch_tool,
    extract_message_content,
    accumulate_usage,
)


class TestAgentStep:
    def test_defaults(self):
        step = AgentStep(step_type="output", content="hello")
        assert step.step_type == "output"
        assert step.content == "hello"
        assert step.tool_name == ""
        assert step.duration_ms == 0

    def test_tool_call_step(self):
        step = AgentStep(
            step_type="tool_call", content="result", tool_name="get_ontology", duration_ms=42
        )
        assert step.tool_name == "get_ontology"
        assert step.duration_ms == 42

    def test_is_dataclass(self):
        step = AgentStep(step_type="output", content="x")
        d = asdict(step)
        assert d == {"step_type": "output", "content": "x", "tool_name": "", "duration_ms": 0}


class TestCallServingEndpoint:
    """The chat-completions transport.

    ``call_serving_endpoint`` now defaults to the Responses API, because
    reasoning models reject function tools on chat-completions. These tests
    describe the chat path specifically, so they pin it rather than inherit
    whatever the default happens to be. ``TestCallServingEndpointResponses``
    below covers the default.
    """

    @pytest.fixture(autouse=True)
    def _force_chat_api(self, monkeypatch):
        monkeypatch.setenv("ONTOBRICKS_LLM_API", "chat")

    @patch("agents.engine_base.call_llm_with_retry")
    def test_builds_url_and_calls(self, mock_retry):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"choices": [{"message": {"content": "hi"}}]}
        mock_retry.return_value = mock_resp

        result = call_serving_endpoint(
            "https://host.databricks.com",
            "tok",
            "my-endpoint",
            [{"role": "user", "content": "hello"}],
        )

        mock_retry.assert_called_once()
        call_args = mock_retry.call_args
        assert "my-endpoint/invocations" in call_args[0][0]
        assert call_args[0][1]["Authorization"] == "Bearer tok"
        assert result == {"choices": [{"message": {"content": "hi"}}]}

    @patch("agents.engine_base.call_llm_with_retry")
    def test_includes_tools_when_provided(self, mock_retry):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_retry.return_value = mock_resp

        tools = [{"type": "function", "function": {"name": "get_data"}}]
        call_serving_endpoint(
            "https://host.databricks.com/",
            "tok",
            "ep",
            [],
            tools=tools,
        )

        payload = mock_retry.call_args[0][2]
        assert payload["tools"] == tools

    @patch("agents.engine_base.call_llm_with_retry")
    def test_no_tools_key_when_none(self, mock_retry):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_retry.return_value = mock_resp

        call_serving_endpoint("https://h", "t", "ep", [])
        payload = mock_retry.call_args[0][2]
        assert "tools" not in payload

    @patch("agents.engine_base.call_llm_with_retry")
    def test_strips_trailing_slash(self, mock_retry):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_retry.return_value = mock_resp

        call_serving_endpoint("https://host.com/", "t", "ep", [])
        url = mock_retry.call_args[0][0]
        assert "//serving" not in url


class TestCallServingEndpointResponses:
    """The Responses transport, which is the default.

    Reasoning models such as databricks-gpt-5-6-sol answer a chat-completions
    request carrying ``tools`` with HTTP 400, and each engine reads that as "no
    tool support" and retries without tools -- disabling the agent loop. These
    tests lock in the request shape that avoids it, and that callers still
    receive the chat-completions reply shape they parse.
    """

    @pytest.fixture(autouse=True)
    def _default_api(self, monkeypatch):
        monkeypatch.delenv("ONTOBRICKS_LLM_API", raising=False)

    @patch("agents.engine_base.call_llm_with_retry")
    def test_posts_to_gateway_responses_path_with_model_in_body(self, mock_retry):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"output": []}
        mock_retry.return_value = mock_resp

        call_serving_endpoint(
            "https://host.databricks.com",
            "tok",
            "my-endpoint",
            [{"role": "user", "content": "hello"}],
        )

        url, headers, payload = mock_retry.call_args[0][:3]
        assert url == "https://host.databricks.com/ai-gateway/mlflow/v1/responses"
        # The endpoint name moves out of the path and into the body.
        assert "my-endpoint" not in url
        assert payload["model"] == "my-endpoint"
        assert headers["Authorization"] == "Bearer tok"

    @patch("agents.engine_base.call_llm_with_retry")
    def test_sends_input_not_messages_and_never_temperature(self, mock_retry):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"output": []}
        mock_retry.return_value = mock_resp

        call_serving_endpoint(
            "https://h", "t", "ep", [{"role": "user", "content": "hi"}], temperature=0.1
        )

        payload = mock_retry.call_args[0][2]
        assert "messages" not in payload
        assert payload["input"] == [
            {"role": "user", "content": [{"type": "input_text", "text": "hi"}]}
        ]
        # Rejected by the Responses API for these models -- sending it would
        # reintroduce the 400 this transport exists to avoid.
        assert "temperature" not in payload
        assert "max_tokens" not in payload

    @patch("agents.engine_base.call_llm_with_retry")
    def test_tools_are_flattened(self, mock_retry):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"output": []}
        mock_retry.return_value = mock_resp

        call_serving_endpoint(
            "https://h", "t", "ep", [],
            tools=[{"type": "function", "function": {"name": "get_data", "parameters": {}}}],
        )

        payload = mock_retry.call_args[0][2]
        # An empty/absent parameters schema is normalised to a valid empty object
        # schema rather than passed through as {}.
        assert payload["tools"] == [
            {
                "type": "function",
                "name": "get_data",
                "description": "",
                "parameters": {"type": "object", "properties": {}},
            }
        ]

    @patch("agents.engine_base.call_llm_with_retry")
    def test_reply_is_translated_to_chat_shape(self, mock_retry):
        """Engines read choices[0].message.tool_calls; they must keep working."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "output": [
                {"type": "reasoning", "id": "rs_1"},
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "get_data",
                    "arguments": "{}",
                },
            ],
            "usage": {"input_tokens": 7, "output_tokens": 2},
        }
        mock_retry.return_value = mock_resp

        result = call_serving_endpoint("https://h", "t", "ep", [])

        message = result["choices"][0]["message"]
        assert message["tool_calls"][0]["function"]["name"] == "get_data"
        assert result["choices"][0]["finish_reason"] == "tool_calls"
        assert result["usage"]["prompt_tokens"] == 7


class TestDispatchTool:
    def test_known_tool(self):
        ctx = MagicMock()
        handlers = {"my_tool": lambda c, **kw: json.dumps({"ok": True})}
        result = dispatch_tool(handlers, ctx, "my_tool", {})
        assert json.loads(result) == {"ok": True}

    def test_unknown_tool(self):
        result = dispatch_tool({}, MagicMock(), "missing_tool", {})
        parsed = json.loads(result)
        assert "error" in parsed
        assert "Unknown tool" in parsed["error"]

    def test_exception_in_handler(self):
        def bad_handler(ctx, **kw):
            raise RuntimeError("boom")

        result = dispatch_tool({"bad": bad_handler}, MagicMock(), "bad", {})
        parsed = json.loads(result)
        assert "error" in parsed
        assert "boom" in parsed["error"]

    def test_passes_kwargs(self):
        def echo_handler(ctx, **kwargs):
            return json.dumps(kwargs)

        result = dispatch_tool(
            {"echo": echo_handler}, MagicMock(), "echo", {"a": 1, "b": "two"}
        )
        assert json.loads(result) == {"a": 1, "b": "two"}


class TestExtractMessageContent:
    def test_openai_format(self):
        resp = {"choices": [{"message": {"content": "Hello world"}}]}
        assert extract_message_content(resp) == "Hello world"

    def test_predictions_format_string(self):
        resp = {"predictions": ["predicted text"]}
        assert extract_message_content(resp) == "predicted text"

    def test_predictions_format_non_string(self):
        resp = {"predictions": [42]}
        assert extract_message_content(resp) == "42"

    def test_empty_choices(self):
        assert extract_message_content({"choices": []}) == ""

    def test_no_content_key(self):
        resp = {"choices": [{"message": {}}]}
        assert extract_message_content(resp) == ""

    def test_unknown_format(self):
        assert extract_message_content({"unknown": 1}) == ""

    def test_none_content(self):
        resp = {"choices": [{"message": {"content": None}}]}
        assert extract_message_content(resp) == ""


class TestAccumulateUsage:
    def test_from_empty(self):
        total = {}
        accumulate_usage(total, {"prompt_tokens": 10, "completion_tokens": 5})
        assert total == {"prompt_tokens": 10, "completion_tokens": 5}

    def test_accumulates(self):
        total = {"prompt_tokens": 10, "completion_tokens": 5}
        accumulate_usage(total, {"prompt_tokens": 20, "completion_tokens": 15})
        assert total == {"prompt_tokens": 30, "completion_tokens": 20}

    def test_missing_keys(self):
        total = {"prompt_tokens": 10}
        accumulate_usage(total, {})
        assert total["prompt_tokens"] == 10
        assert total["completion_tokens"] == 0

    def test_empty_usage_block(self):
        total = {}
        accumulate_usage(total, {})
        assert total == {"prompt_tokens": 0, "completion_tokens": 0}
