"""The only module that talks to the Anthropic API.

Keeping the SDK behind this small interface means the agent loop and guardrails
can be unit-tested with a scripted fake (tests/fakes.py) and never need network
access.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

import anthropic
from anthropic.types.beta import BetaMessageParam, BetaTextBlockParam, BetaToolParam
from pydantic import BaseModel, Field

from .config import Settings

log = logging.getLogger(__name__)

TextCallback = Callable[[str], Awaitable[None]]


class LLMError(Exception):
    def __init__(self, user_safe: str, *, retryable: bool = False):
        super().__init__(user_safe)
        self.user_safe = user_safe
        self.retryable = retryable


@dataclass
class ToolCall:
    id: str
    name: str
    input: Any


@dataclass
class Turn:
    # Exactly what the API returned. It is appended to history unchanged so
    # thinking blocks stay valid on the next request.
    content: list[Any]
    stop_reason: str | None
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


class GuardrailDecision(BaseModel):
    decision: Literal["answer", "chitchat", "out_of_dataset", "off_topic", "unsafe"]
    reason: str = Field(description="One short sentence explaining the decision, for logs.")
    reply: str = Field(description="What to say to the user. Empty when decision is 'answer'.")


class GroundingVerdict(BaseModel):
    supported: bool = Field(description="True if every data claim in the answer is supported by the query results.")
    unsupported_claims: list[str] = Field(default_factory=list, description="Each unsupported claim, quoting the figure.")
    reason: str = Field(description="One short sentence, for logs.")


class LLM(Protocol):
    async def agent_turn(
        self, *, system: str, messages: list[dict], tools: list[dict], on_text: TextCallback, memory: str = ""
    ) -> Turn: ...

    async def classify(self, *, system: str, transcript: str) -> GuardrailDecision: ...

    async def verify(self, *, system: str, payload: str) -> GroundingVerdict: ...


class AnthropicLLM:
    def __init__(self, settings: Settings):
        self._s = settings
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key or None,
            timeout=anthropic.Timeout(settings.request_deadline_s, connect=5.0),
            max_retries=2,
        )

    async def agent_turn(
        self, *, system: str, messages: list[dict], tools: list[dict], on_text: TextCallback, memory: str = ""
    ) -> Turn:
        # The stable prompt is cached; per-user memories come after the cache breakpoint,
        # so saving a memory doesn't invalidate the cached schema overview.
        system_blocks: list[BetaTextBlockParam] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if memory:
            system_blocks.append({"type": "text", "text": memory})
        try:
            async with self._client.beta.messages.stream(
                model=self._s.agent_model,
                max_tokens=16000,
                system=system_blocks,
                messages=cast("list[BetaMessageParam]", messages),
                tools=cast("list[BetaToolParam]", tools),
                thinking={"type": "adaptive"},
                output_config={"effort": self._s.agent_effort},
                # If a safety classifier declines, the API re-runs the request on a fallback model
                # instead of returning a refusal.
                betas=["server-side-fallback-2026-07-01", "compact-2026-01-12"],
                fallbacks="default",
                # Long conversations are summarised server-side instead of hitting the context limit.
                # Compaction blocks come back in the content and are stored and resent unchanged.
                context_management={"edits": [{"type": "compact_20260112"}]},
            ) as stream:
                async for event in stream:
                    if event.type == "text":
                        await on_text(event.text)
                msg = await stream.get_final_message()
        except anthropic.RateLimitError as e:
            raise LLMError(
                "The language model is busy right now (rate limited). Please try again in a minute.", retryable=True
            ) from e
        except anthropic.APITimeoutError as e:
            raise LLMError("The language model took too long to respond.", retryable=True) from e
        except anthropic.APIConnectionError as e:
            raise LLMError("I couldn't reach the language model service.", retryable=True) from e
        except anthropic.AuthenticationError as e:
            log.error("anthropic auth failed - check ANTHROPIC_API_KEY")
            raise LLMError("The assistant is misconfigured (model credentials). Please contact the maintainer.") from e
        except anthropic.APIStatusError as e:
            log.exception("anthropic status error")
            raise LLMError(f"The language model returned an error ({e.status_code}).", retryable=e.status_code >= 500) from e
        except ValueError as e:
            # The SDK raises ValueError when a streamed tool input is not parseable JSON.
            log.warning("unparseable tool input from model: %s", e)
            raise LLMError("The model produced a malformed tool call.", retryable=True) from e

        calls = [ToolCall(b.id, b.name, b.input) for b in msg.content if b.type == "tool_use"]
        text = "".join(b.text for b in msg.content if b.type == "text")
        u = msg.usage
        usage = {
            "input": u.input_tokens,
            "output": u.output_tokens,
            "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
        }
        # Streamed messages carry no _request_id; the message id is enough to find the call in the Console.
        log.info("model step", extra={"stop_reason": msg.stop_reason, "anthropic_message_id": msg.id, "tokens": usage})
        return Turn(content=list(msg.content), stop_reason=msg.stop_reason, text=text, tool_calls=calls, usage=usage)

    async def classify(self, *, system: str, transcript: str) -> GuardrailDecision:
        resp = await self._client.with_options(timeout=15.0, max_retries=1).messages.parse(
            model=self._s.guardrail_model,
            max_tokens=2000,
            system=system,
            messages=[{"role": "user", "content": transcript}],
            output_format=GuardrailDecision,
            output_config={"effort": self._s.guardrail_effort},
        )
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            # A small classifier refusing is weak evidence (e.g. a legitimate question about
            # race and income); raise so guardrails.classify fails open and the agent, which
            # has its own safety handling and a refusal fallback, makes the call.
            raise LLMError(f"guardrail classifier returned no decision (stop_reason={resp.stop_reason})")
        return resp.parsed_output

    async def verify(self, *, system: str, payload: str) -> GroundingVerdict:
        resp = await self._client.with_options(timeout=20.0, max_retries=1).messages.parse(
            model=self._s.guardrail_model,
            max_tokens=4000,
            system=system,
            messages=[{"role": "user", "content": payload}],
            output_format=GroundingVerdict,
            output_config={"effort": self._s.verifier_effort},
        )
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            raise LLMError(f"grounding verifier returned no verdict (stop_reason={resp.stop_reason})")
        return resp.parsed_output
