"""The agent loop: guardrail -> (model <-> tools)* -> grounding checks -> answer.

The loop is written by hand instead of using the SDK tool runner because we need
control over each step: a wall-clock deadline per user turn, progress events for
the UI, a step cap, tools run in parallel, and a turn that fails partway must be
discarded atomically so the stored history stays valid.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from . import guardrails
from .config import Settings
from .llm import LLM, LLMError, ToolCall, Turn
from .sessions import Session
from .tools import TOOL_DEFS, ToolExecutor, ToolOutput
from .warehouse import WarehouseUnavailable

log = logging.getLogger(__name__)

Emit = Callable[[str, dict], Awaitable[None]]

GROUNDING_NUDGE = (
    "[Automated grounding check] Your answer contains {problem}. Run the queries needed to support them, or correct "
    "or remove them and say what you could not verify. Then give the full corrected answer. Do not mention this check."
)
WRAP_UP = (
    "You are out of time or tool steps for this question. Do not call any more tools. Answer now using only the query "
    "results you already have, and say clearly what you could not finish."
)
MSG_WAREHOUSE_DOWN = (
    "I'm having trouble connecting to the census database right now, so I can't look that up. Please try again in a minute."
)
MSG_TIMEOUT = "Sorry, that question took too long to answer. Try narrowing it (one state or county, or one measure at a time)."
MSG_REFUSAL = "Sorry, I can't help with that request. I can answer questions about US population data."
MSG_TOO_LONG = "This conversation has reached its length limit. Please start a new chat to keep going."


@dataclass
class TurnStats:
    decision: str = ""
    steps: int = 0
    queries_ok: int = 0
    queries_failed: int = 0
    grounding_retry: bool = False
    verifier: str = "not_run"  # not_run | pass | fail | error | skipped_deadline
    usage: dict[str, int] = field(default_factory=dict)
    outcome: str = "ok"

    def add_usage(self, u: dict[str, int]) -> None:
        for k, v in u.items():
            self.usage[k] = self.usage.get(k, 0) + v


class Agent:
    def __init__(self, llm: LLM, tools: ToolExecutor, system_prompt: str, settings: Settings):
        self.llm = llm
        self.tools = tools
        self.system = system_prompt
        self.s = settings

    async def answer(self, session: Session, raw_question: str, emit: Emit) -> TurnStats:
        stats = TurnStats()
        started = time.monotonic()
        deadline = started + self.s.request_deadline_s

        try:
            question = guardrails.precheck(raw_question, max_chars=self.s.max_question_chars)
        except guardrails.InputRejected as e:
            stats.outcome = "precheck_rejected"
            await emit("delta", {"text": e.message})
            await emit("done", {"outcome": stats.outcome})
            return stats

        if session.user_turns >= self.s.max_history_turns:
            stats.outcome = "too_long"
            await emit("delta", {"text": MSG_TOO_LONG})
            await emit("done", {"outcome": stats.outcome})
            return stats

        # ---- 1. input guardrail -------------------------------------------------
        await emit("status", {"text": "Checking your question…"})
        decision = await guardrails.classify(self.llm, session.transcript, question)
        stats.decision = decision.decision
        if decision.decision != "answer":
            reply = decision.reply.strip() or MSG_REFUSAL
            stats.outcome = f"guardrail_{decision.decision}"
            self._commit_plain(session, question, reply)
            await emit("delta", {"text": reply})
            await emit("done", {"outcome": stats.outcome, "decision": decision.decision})
            log.info("guardrail rejected", extra={"decision": decision.decision, "reason": decision.reason})
            return stats

        # ---- 2. agent loop -------------------------------------------------------
        messages: list[dict] = [*session.api_messages, {"role": "user", "content": question}]
        evidence = _evidence(session.api_messages)
        final_text = ""
        wrap_up_sent = False
        grounding_checks = 0
        queried_this_turn = False
        turn_results: list[str] = []  # successful tool results this turn, for the verifier

        async def on_text(t: str) -> None:
            await emit("delta", {"text": t})

        try:
            while True:
                remaining = deadline - time.monotonic()
                out_of_budget = stats.steps >= self.s.max_agent_steps or remaining < 12
                if out_of_budget and not wrap_up_sent:
                    if messages[-1]["role"] == "assistant":
                        break  # nothing pending; keep what we have
                    messages.append({"role": "system", "content": WRAP_UP})
                    wrap_up_sent = True
                if remaining < 3:
                    raise TimeoutError

                stats.steps += 1
                turn: Turn = await asyncio.wait_for(
                    self.llm.agent_turn(system=self.system, messages=messages, tools=TOOL_DEFS, on_text=on_text),
                    timeout=remaining,
                )
                stats.add_usage(turn.usage)
                messages.append({"role": "assistant", "content": turn.content})

                if turn.stop_reason == "refusal":
                    stats.outcome = "model_refusal"
                    final_text = turn.text.strip() or MSG_REFUSAL
                    await emit("replace", {"text": final_text})
                    break

                if turn.stop_reason == "tool_use" and turn.tool_calls and not wrap_up_sent:
                    if turn.text.strip():
                        await emit("reset", {})  # pre-tool narration isn't the answer
                    results = await self._run_tools(turn, emit, stats)
                    # Every successful tool result is evidence: query rows, but also e.g. how many
                    # places a name matched (a live eval caught the verifier flagging that as unsupported).
                    for c, r in zip(turn.tool_calls, results, strict=True):
                        if r.is_error:
                            continue
                        if c.name == "run_sql":
                            purpose = c.input.get("purpose", "") if isinstance(c.input, dict) else ""
                            turn_results.append(f"run_sql ({purpose}):\n{r.meta.get('sql', '')}\nResult: {r.content}")
                        else:
                            turn_results.append(f"{c.name}({json.dumps(c.input)[:200]}):\n{r.content}")
                    queried_this_turn = queried_this_turn or any(
                        c.name == "run_sql" and not r.is_error for c, r in zip(turn.tool_calls, results, strict=True)
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": [
                                {"type": "tool_result", "tool_use_id": c.id, "content": r.content, "is_error": r.is_error}
                                for c, r in zip(turn.tool_calls, results, strict=True)
                            ],
                        }
                    )
                    continue

                if turn.stop_reason == "tool_use":
                    # Model ignored the wrap-up instruction; answer the calls with an error and stop.
                    messages.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": c.id,
                                    "is_error": True,
                                    "content": "Out of tool budget; no more tool calls this turn.",
                                }
                                for c in turn.tool_calls
                            ],
                        }
                    )
                    final_text = turn.text.strip() or MSG_TIMEOUT
                    messages.append({"role": "assistant", "content": final_text})
                    stats.outcome = "step_limit"
                    break

                final_text = turn.text.strip()
                if turn.stop_reason == "max_tokens":
                    final_text += "\n\n_(Answer truncated.)_"
                    await emit("delta", {"text": "\n\n_(Answer truncated.)_"})

                # ---- 3. output grounding checks ------------------------------------
                # (a) free heuristic: large figures with no query at all; (b) LLM verifier for
                # every answer that states figures. One retry, then a visible caveat.
                if grounding_checks < 2:
                    grounding_checks += 1
                    problem = await self._grounding_problem(
                        question, final_text, turn_results, evidence, queried_this_turn, deadline, emit, stats
                    )
                    if problem and grounding_checks == 1 and deadline - time.monotonic() > 20:
                        stats.grounding_retry = True
                        log.warning("grounding check failed; retrying", extra={"problem": problem})
                        await emit("reset", {})
                        await emit("status", {"text": "Double-checking figures against the data…"})
                        messages.append({"role": "user", "content": GROUNDING_NUDGE.format(problem=problem)})
                        continue
                    if problem:
                        log.warning("grounding check failed; answering with caveat", extra={"problem": problem})
                        note = "\n\n_Note: an automated check couldn't confirm every figure above against the query results._"
                        final_text += note
                        await emit("delta", {"text": note})
                break

            if not final_text:
                final_text = "I wasn't able to produce an answer for that. Could you rephrase or narrow the question?"
                await emit("replace", {"text": final_text})
            session.commit(messages, question, final_text)

        except asyncio.CancelledError:
            # The user pressed Stop or closed the tab. Nothing was committed, so the
            # conversation stays as it was before this question.
            log.info("turn cancelled by client", extra={"steps": stats.steps, "elapsed_s": round(time.monotonic() - started, 2)})
            raise
        except WarehouseUnavailable as e:
            stats.outcome = "warehouse_unavailable"
            await self._fail(session, question, e.user_safe or MSG_WAREHOUSE_DOWN, emit)
        except TimeoutError:
            stats.outcome = "timeout"
            await self._fail(session, question, MSG_TIMEOUT, emit)
        except LLMError as e:
            stats.outcome = "llm_error"
            msg = e.user_safe + (" Please try again." if e.retryable else "")
            await self._fail(session, question, msg, emit)
        except Exception:
            log.exception("unexpected agent failure")
            stats.outcome = "internal_error"
            await self._fail(session, question, "Something went wrong on my side while answering. Please try again.", emit)

        elapsed = round(time.monotonic() - started, 2)
        await emit("done", {"outcome": stats.outcome, "elapsed_s": elapsed, "steps": stats.steps})
        log.info(
            "turn complete",
            extra={
                "outcome": stats.outcome,
                "decision": stats.decision,
                "steps": stats.steps,
                "sql_ok": stats.queries_ok,
                "sql_failed": stats.queries_failed,
                "grounding_retry": stats.grounding_retry,
                "verifier": stats.verifier,
                "elapsed_s": elapsed,
                "tokens": stats.usage,
            },
        )
        return stats

    # ------------------------------------------------------------------------
    async def _run_tools(self, turn: Turn, emit: Emit, stats: TurnStats) -> list[ToolOutput]:
        for c in turn.tool_calls:
            await emit("status", {"text": _status_for(c.name, c.input)})

        async def one(c: ToolCall) -> ToolOutput:
            out = await self.tools.run(c.name, c.input)
            if c.name == "run_sql":
                if out.is_error:
                    stats.queries_failed += 1
                else:
                    stats.queries_ok += 1
                if out.meta.get("sql"):
                    await emit(
                        "sql",
                        {
                            "sql": out.meta["sql"],
                            "purpose": out.meta.get("purpose", ""),
                            "ok": not out.is_error,
                            "rows": out.meta.get("rows"),
                            "ms": out.meta.get("ms"),
                            "capped": out.meta.get("capped", False),
                        },
                    )
            return out

        results = await asyncio.gather(*(one(c) for c in turn.tool_calls), return_exceptions=True)
        for r in results:
            if isinstance(r, WarehouseUnavailable):
                raise r
        final: list[ToolOutput] = []
        for c, r in zip(turn.tool_calls, results, strict=True):
            if isinstance(r, BaseException):
                log.error("tool %s crashed", c.name, exc_info=r)
                final.append(ToolOutput(json.dumps({"error": "Internal tool error."}), is_error=True))
            else:
                final.append(r)
        return final

    async def _grounding_problem(
        self,
        question: str,
        answer: str,
        turn_results: list[str],
        evidence: str,
        queried_this_turn: bool,
        deadline: float,
        emit: Emit,
        stats: TurnStats,
    ) -> str | None:
        """A description of what's unsupported in `answer`, or None if it checks out (or couldn't be checked)."""
        flagged = guardrails.ungrounded_figures(answer, queried_this_turn=queried_this_turn, evidence=evidence)
        if flagged:
            return f"figures ({', '.join(flagged[:5])}) that did not come from any query result in this conversation"
        if not guardrails.needs_verification(answer):
            return None
        remaining = deadline - time.monotonic()
        if remaining < 8:
            stats.verifier = "skipped_deadline"
            return None
        await emit("status", {"text": "Checking figures against the query results…"})
        verdict = await guardrails.verify_grounding(
            self.llm,
            question=question,
            answer=answer,
            turn_results=turn_results,
            prior_evidence=evidence,
            timeout_s=min(20.0, remaining - 3),
        )
        if verdict is None:
            stats.verifier = "error"
            return None
        stats.verifier = "pass" if verdict.supported else "fail"
        if verdict.supported:
            return None
        claims = "; ".join(verdict.unsupported_claims[:5]) or verdict.reason
        return f"claims the query results don't support ({claims})"

    def _commit_plain(self, session: Session, question: str, answer: str) -> None:
        session.commit(
            [*session.api_messages, {"role": "user", "content": question}, {"role": "assistant", "content": answer}],
            question,
            answer,
        )

    async def _fail(self, session: Session, question: str, message: str, emit: Emit) -> None:
        # Drop the partial turn (it may end in an unanswered tool_use) and record a plain exchange instead.
        self._commit_plain(session, question, message)
        await emit("replace", {"text": message})


def _status_for(name: str, args: Any) -> str:
    a = args if isinstance(args, dict) else {}
    if name == "search_fields":
        return f"Searching census fields for “{str(a.get('query', ''))[:60]}”…"
    if name == "describe_table":
        return f"Reading table {a.get('table_number', '')}…"
    if name == "resolve_geography":
        return f"Looking up “{str(a.get('place', ''))[:60]}”…"
    if name == "run_sql":
        return f"Querying: {str(a.get('purpose', 'running query'))[:80]}…"
    return "Working…"


def _evidence(messages: list[dict]) -> str:
    """Concatenate prior tool results and assistant text, for the grounding check."""
    parts: list[str] = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
            continue
        for b in c or []:
            if isinstance(b, dict):
                if b.get("type") == "tool_result":
                    parts.append(str(b.get("content", "")))
                elif b.get("type") == "text":
                    parts.append(b.get("text", ""))
            elif getattr(b, "type", None) == "text":
                parts.append(b.text)
    return "\n".join(parts)
