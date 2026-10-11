"""AnthropicLLM against a fake SDK client.

The agent tests use a scripted LLM, so they never touch this wrapper. These tests
pin down the wrapper's own contract: it streams text, returns tool calls, reports
usage, maps SDK errors to user-safe messages, and doesn't depend on attributes
that streamed messages lack (a live run failed once on `_request_id`).
"""

from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from app.config import Settings
from app.llm import AnthropicLLM, LLMError

pytestmark = pytest.mark.asyncio


def final_message(*blocks, stop="end_turn"):
    # Deliberately no _request_id: streamed final messages don't have one.
    return SimpleNamespace(
        id="msg_test",
        content=list(blocks),
        stop_reason=stop,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=100, cache_creation_input_tokens=0),
    )


class FakeStream:
    def __init__(self, events, message=None, error=None):
        self.events, self.message, self.error = events, message, error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for e in self.events:
                yield e

        return gen()

    async def get_final_message(self):
        return self.message


def llm_with(stream: FakeStream) -> tuple[AnthropicLLM, dict]:
    llm = AnthropicLLM(Settings(_env_file=None, anthropic_api_key="test"))
    seen: dict = {}

    def fake_stream(**kwargs):
        seen.update(kwargs)
        return stream

    llm._client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=fake_stream)))
    return llm, seen


async def test_streams_text_and_returns_turn():
    msg = final_message(SimpleNamespace(type="text", text="Hello"))
    llm, seen = llm_with(FakeStream([SimpleNamespace(type="text", text="Hel"), SimpleNamespace(type="text", text="lo")], msg))
    chunks: list[str] = []

    async def on_text(t):
        chunks.append(t)

    turn = await llm.agent_turn(system="S", messages=[{"role": "user", "content": "hi"}], tools=[], on_text=on_text)
    assert chunks == ["Hel", "lo"] and turn.text == "Hello" and turn.stop_reason == "end_turn"
    assert turn.usage == {"input": 10, "output": 5, "cache_read": 100, "cache_write": 0}
    # Request shape: cached system prompt, adaptive thinking, refusal fallback.
    assert seen["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert seen["thinking"] == {"type": "adaptive"} and seen["fallbacks"] == "default"


async def test_returns_tool_calls():
    msg = final_message(SimpleNamespace(type="tool_use", id="t1", name="run_sql", input={"sql": "x"}), stop="tool_use")
    llm, _ = llm_with(FakeStream([], msg))

    async def on_text(t):
        pass

    turn = await llm.agent_turn(system="S", messages=[], tools=[], on_text=on_text)
    assert [(c.id, c.name, c.input) for c in turn.tool_calls] == [("t1", "run_sql", {"sql": "x"})]


def _status_error(cls, code):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(code, request=req), body=None)


@pytest.mark.parametrize(
    "error, message, retryable",
    [
        (_status_error(anthropic.RateLimitError, 429), "busy", True),
        (_status_error(anthropic.AuthenticationError, 401), "misconfigured", False),
        (_status_error(anthropic.InternalServerError, 500), "(500)", True),
        (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")), "couldn't reach", True),
    ],
)
async def test_sdk_errors_become_user_safe(error, message, retryable):
    llm, _ = llm_with(FakeStream([], error=error))

    async def on_text(t):
        pass

    with pytest.raises(LLMError) as ei:
        await llm.agent_turn(system="S", messages=[], tools=[], on_text=on_text)
    assert message in ei.value.user_safe and ei.value.retryable is retryable
