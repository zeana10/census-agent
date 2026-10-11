"""Validation layer around the agent.

Input side:
  1. cheap deterministic checks (empty / too long / control characters);
  2. an LLM classifier that routes each message: answer, chitchat, out_of_dataset,
     off_topic, unsafe. Non-"answer" decisions reply immediately without touching
     the database (the fast-fail path).

Output side:
  3. `ungrounded_figures`: a free, instant check that flags large figures stated
     when no query ran at all (the "answering from memory" failure).
  4. `verify_grounding`: an LLM verifier (small model) that checks every data claim
     in the answer against this turn's query results and earlier results, allowing
     for arithmetic derivations. Unsupported claims get the agent one retry, then a
     visible caveat.

The classifier and the verifier both fail open. If it errors or times out we still run the agent,
whose system prompt and read-only tools enforce the same scope. Blocking every
user whenever the classifier has a hiccup would be the worse failure for a demo.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata

from .llm import LLM, GroundingVerdict, GuardrailDecision
from .prompts import GROUNDING_SYSTEM, GUARDRAIL_SYSTEM

log = logging.getLogger(__name__)


class InputRejected(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def precheck(text: str, *, max_chars: int) -> str:
    cleaned = "".join(ch for ch in (text or "") if ch in "\n\t" or unicodedata.category(ch)[0] != "C").strip()
    if not cleaned:
        raise InputRejected("Please type a question about US population data.")
    if len(cleaned) > max_chars:
        raise InputRejected(
            f"That message is too long ({len(cleaned):,} characters). Please keep questions under {max_chars:,} characters."
        )
    return cleaned


async def classify(llm: LLM, transcript: list[tuple[str, str]], question: str, *, timeout_s: float = 15.0) -> GuardrailDecision:
    recent = transcript[-6:]
    lines = [f"{role.upper()}: {text[:600]}" for role, text in recent]
    rendered = (
        "<conversation>\n" + "\n".join(lines) + "\n</conversation>\n\n" if lines else ""
    ) + f"<latest_user_message>\n{question}\n</latest_user_message>"
    try:
        return await asyncio.wait_for(llm.classify(system=GUARDRAIL_SYSTEM, transcript=rendered), timeout_s)
    except Exception as e:  # fail open, see module docstring
        log.warning("guardrail classifier failed (%s: %s); failing open", type(e).__name__, e)
        return GuardrailDecision(decision="answer", reason=f"classifier error: {type(e).__name__}", reply="")


_BIG_NUMBER = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d{5,})(?:\.\d+)?(?![\w])|\b\d+(?:\.\d+)?\s*(?:million|billion|thousand)\b", re.I
)


def ungrounded_figures(answer: str, *, queried_this_turn: bool, evidence: str) -> list[str]:
    """Large figures in `answer` that appear nowhere in `evidence` (prior tool results / answers).

    Only applies when no query succeeded this turn. Percentages and small numbers
    are ignored because they are often derived from earlier results. This is a
    cheap heuristic against "answering from memory", not a full fact-checker.
    """
    if queried_this_turn:
        return []
    ev = evidence.replace(",", "")
    out = []
    for m in _BIG_NUMBER.finditer(answer):
        token = m.group(0)
        digits = token.replace(",", "").split(".")[0]
        if digits.isdigit() and digits in ev:
            continue
        out.append(token)
    return out


_MAX_TURN_EVIDENCE = 24_000
_MAX_PRIOR_EVIDENCE = 12_000


def needs_verification(answer: str) -> bool:
    """Only answers that state figures are verified; clarifying questions and refusals skip the cost."""
    return bool(re.search(r"\d", answer))


async def verify_grounding(
    llm: LLM, *, question: str, answer: str, turn_results: list[str], prior_evidence: str, timeout_s: float = 20.0
) -> GroundingVerdict | None:
    """None means the verifier couldn't run (error/timeout); the caller treats that as 'not checked'."""
    turn = "\n\n".join(turn_results)[-_MAX_TURN_EVIDENCE:] or "(no tools were run this turn)"
    prior = prior_evidence[-_MAX_PRIOR_EVIDENCE:] or "(none)"
    payload = (
        f"<question>\n{question}\n</question>\n\n"
        f"<tool_results_this_turn>\n{turn}\n</tool_results_this_turn>\n\n"
        f"<earlier_results_and_answers>\n{prior}\n</earlier_results_and_answers>\n\n"
        f"<draft_answer>\n{answer}\n</draft_answer>"
    )
    try:
        return await asyncio.wait_for(llm.verify(system=GROUNDING_SYSTEM, payload=payload), timeout_s)
    except Exception as e:  # fail open, see module docstring
        log.warning("grounding verifier failed (%s: %s); answer not verified", type(e).__name__, e)
        return None
