"""Agent-loop behaviour with a scripted model and a real (DuckDB) database."""

import asyncio
import json

import pytest

from app import guardrails
from app.agent import MSG_TIMEOUT, MSG_WAREHOUSE_DOWN, Agent
from app.llm import GuardrailDecision, LLMError
from app.sessions import Session
from app.warehouse import WarehouseUnavailable

from .fakes import ScriptedLLM, text_turn, tool_turn

pytestmark = pytest.mark.asyncio

TX_SQL = 'SELECT SUM("B01003e1") AS pop FROM "2019_CBG_B01" WHERE LEFT("CENSUS_BLOCK_GROUP", 2) = \'48\''


class Recorder:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, ev, data):
        self.events.append((ev, data))

    def of(self, ev):
        return [d for e, d in self.events if e == ev]

    @property
    def visible_text(self) -> str:
        """Reconstruct what the UI shows, applying reset/replace semantics."""
        text = ""
        for e, d in self.events:
            if e == "delta":
                text += d["text"]
            elif e == "reset":
                text = ""
            elif e == "replace":
                text = d["text"]
        return text


def make_agent(llm, executor, settings):
    return Agent(llm, executor, "SYSTEM", settings)


def assert_valid_history(messages):
    """Roles alternate correctly and every tool_use has a matching tool_result."""
    pending: set[str] = set()
    for m in messages:
        content = m["content"] if isinstance(m["content"], list) else []
        if m["role"] == "assistant":
            assert not pending, f"unanswered tool calls {pending}"
            pending = {b.id for b in content if getattr(b, "type", None) == "tool_use"}
        elif m["role"] == "user" and pending:
            got = {b["tool_use_id"] for b in content if isinstance(b, dict) and b.get("type") == "tool_result"}
            assert got == pending
            pending = set()
    assert not pending


async def test_happy_path_queries_then_answers(executor, settings):
    llm = ScriptedLLM(
        steps=[
            tool_turn(("resolve_geography", {"place": "Texas"}), ("search_fields", {"query": "total population"})),
            tool_turn(("run_sql", {"sql": TX_SQL, "purpose": "Texas population"})),
            text_turn("Texas has **6,000** people.\n\nSource: B01003e1"),
        ]
    )
    session, rec = Session(id="s"), Recorder()
    stats = await make_agent(llm, executor, settings).answer(session, "Population of Texas?", rec)

    assert stats.outcome == "ok" and stats.queries_ok == 1 and stats.steps == 3
    assert rec.visible_text.startswith("Texas has **6,000**")
    assert [d["purpose"] for d in rec.of("sql")] == ["Texas population"]
    assert rec.of("done")[0]["outcome"] == "ok"
    # Tool results reach the model and contain the real number.
    last_req = llm.requests[-1]
    result = json.loads(last_req[-1]["content"][0]["content"])
    assert result["rows"] == [[6000]]
    assert_valid_history(session.api_messages)
    assert session.transcript[-1] == ("assistant", "Texas has **6,000** people.\n\nSource: B01003e1")


async def test_follow_up_sends_prior_turns(executor, settings):
    llm = ScriptedLLM(steps=[text_turn("First answer with no figures."), text_turn("Second answer.")])
    session, agent = Session(id="s"), make_agent(llm, executor, settings)
    await agent.answer(session, "What can you tell me about Texas?", Recorder())
    await agent.answer(session, "What about Washington?", Recorder())
    second = llm.requests[1]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert second[0]["content"] == "What can you tell me about Texas?"
    assert "What can you tell me about Texas?" in llm.classify_inputs[1]  # guardrail sees context too


async def test_guardrail_rejection_skips_agent_and_database(executor, settings, warehouse):
    llm = ScriptedLLM(decision=GuardrailDecision(decision="off_topic", reason="poem", reply="I can only help with census data."))
    session, rec = Session(id="s"), Recorder()
    stats = await make_agent(llm, executor, settings).answer(session, "Write me a poem", rec)
    assert stats.outcome == "guardrail_off_topic"
    assert rec.visible_text == "I can only help with census data."
    assert llm.requests == [] and warehouse.queries == []
    assert session.transcript == [("user", "Write me a poem"), ("assistant", "I can only help with census data.")]


async def test_guardrail_failure_fails_open(executor, settings):
    llm = ScriptedLLM(decision=TimeoutError("slow"), steps=[text_turn("An answer without figures.")])
    stats = await make_agent(llm, executor, settings).answer(Session(id="s"), "Population of Texas?", Recorder())
    assert stats.decision == "answer" and stats.outcome == "ok"


@pytest.mark.parametrize("q, expected", [("", "Please type a question"), ("x" * 5000, "too long"), ("\x00\x01", "Please type")])
async def test_precheck_rejects_bad_input(executor, settings, q, expected):
    llm, rec = ScriptedLLM(), Recorder()
    await make_agent(llm, executor, settings).answer(Session(id="s"), q, rec)
    assert expected in rec.visible_text and llm.requests == [] and llm.classify_inputs == []


async def test_sql_error_lets_model_retry(executor, settings):
    llm = ScriptedLLM(
        steps=[
            tool_turn(("run_sql", {"sql": 'SELECT "B01003X1" FROM "2019_CBG_B01"', "purpose": "bad column"})),
            tool_turn(("run_sql", {"sql": TX_SQL, "purpose": "fixed"})),
            text_turn("6,000 people."),
        ]
    )
    rec = Recorder()
    stats = await make_agent(llm, executor, settings).answer(Session(id="s"), "Population of Texas?", rec)
    assert stats.queries_failed == 1 and stats.queries_ok == 1
    assert [d["ok"] for d in rec.of("sql")] == [False, True]
    err = llm.requests[1][-1]["content"][0]
    assert err["is_error"] is True and "SQL error" in err["content"]


async def test_warehouse_outage_gives_clear_message_and_valid_history(executor, settings, warehouse):
    warehouse.fail_with = WarehouseUnavailable(MSG_WAREHOUSE_DOWN)
    llm = ScriptedLLM(steps=[tool_turn(("run_sql", {"sql": TX_SQL, "purpose": "x"}))])
    session, rec = Session(id="s"), Recorder()
    stats = await make_agent(llm, executor, settings).answer(session, "Population of Texas?", rec)
    assert stats.outcome == "warehouse_unavailable"
    assert rec.visible_text == MSG_WAREHOUSE_DOWN
    # The dangling tool_use is discarded so the next turn's request is valid.
    assert_valid_history(session.api_messages)
    assert session.api_messages[-1] == {"role": "assistant", "content": MSG_WAREHOUSE_DOWN}


async def test_llm_errors_are_user_safe(executor, settings):
    llm = ScriptedLLM(steps=[LLMError("The language model is busy right now (rate limited).", retryable=True)])
    rec = Recorder()
    stats = await make_agent(llm, executor, settings).answer(Session(id="s"), "Population of Texas?", rec)
    assert stats.outcome == "llm_error"
    assert "busy" in rec.visible_text and "try again" in rec.visible_text


async def test_unexpected_exception_does_not_leak(executor, settings):
    llm = ScriptedLLM(steps=[RuntimeError("secret stack detail")])
    rec = Recorder()
    stats = await make_agent(llm, executor, settings).answer(Session(id="s"), "Population of Texas?", rec)
    assert stats.outcome == "internal_error" and "secret" not in rec.visible_text


async def test_deadline_is_enforced(executor, settings):
    settings.request_deadline_s = 0.5
    llm = ScriptedLLM(steps=[text_turn("late")], turn_delay_s=2)
    rec = Recorder()
    stats = await asyncio.wait_for(make_agent(llm, executor, settings).answer(Session(id="s"), "q?", rec), 5)
    assert stats.outcome == "timeout" and rec.visible_text == MSG_TIMEOUT


async def test_step_limit_forces_wrap_up(executor, settings):
    settings.max_agent_steps = 2

    def final(messages):
        assert messages[-1]["role"] == "system" and "out of time or tool steps" in messages[-1]["content"]
        return text_turn("I found the fields but couldn't finish the calculation.")

    llm = ScriptedLLM(
        steps=[
            tool_turn(("search_fields", {"query": "population"})),
            tool_turn(("search_fields", {"query": "population again"})),
            final,
        ]
    )
    session = Session(id="s")
    stats = await make_agent(llm, executor, settings).answer(session, "q?", Recorder())
    assert stats.steps == 3 and "couldn't finish" in session.transcript[-1][1]
    assert_valid_history(session.api_messages)


async def test_grounding_check_retries_when_figures_were_not_queried(executor, settings):
    llm = ScriptedLLM(
        steps=[
            text_turn("Texas has about 29 million people."),  # from memory, no query
            lambda msgs: (
                tool_turn(("run_sql", {"sql": TX_SQL, "purpose": "Texas population"}))
                if "grounding check" in msgs[-1]["content"]
                else text_turn("wrong")
            ),
            text_turn("In this dataset Texas has 6,000 people."),
        ]
    )
    rec = Recorder()
    stats = await make_agent(llm, executor, settings).answer(Session(id="s"), "Population of Texas?", rec)
    assert stats.grounding_retry and stats.queries_ok == 1
    assert rec.visible_text == "In this dataset Texas has 6,000 people."


async def test_grounding_check_allows_figures_from_earlier_turns(executor, settings):
    llm = ScriptedLLM(
        steps=[
            tool_turn(("run_sql", {"sql": TX_SQL, "purpose": "Texas population"})),
            text_turn("Texas has 6,000 people."),
            text_turn("As I said, 6,000 people."),
        ]
    )
    session, agent = Session(id="s"), make_agent(llm, executor, settings)
    await agent.answer(session, "Population of Texas?", Recorder())
    stats = await agent.answer(session, "Say that again?", Recorder())
    assert not stats.grounding_retry


async def test_model_refusal_is_graceful(executor, settings):
    llm = ScriptedLLM(steps=[text_turn("", stop="refusal")])
    rec = Recorder()
    stats = await make_agent(llm, executor, settings).answer(Session(id="s"), "q?", rec)
    assert stats.outcome == "model_refusal" and "can't help" in rec.visible_text


async def test_pre_tool_narration_is_cleared_from_answer(executor, settings):
    llm = ScriptedLLM(
        steps=[
            tool_turn(("search_fields", {"query": "population"}), text="Let me look that up."),
            text_turn("Final answer."),
        ]
    )
    rec = Recorder()
    await make_agent(llm, executor, settings).answer(Session(id="s"), "q?", rec)
    assert rec.visible_text == "Final answer."


async def test_conversation_length_limit(executor, settings):
    settings.max_history_turns = 1
    llm = ScriptedLLM(steps=[text_turn("one")])
    session, agent = Session(id="s"), make_agent(llm, executor, settings)
    await agent.answer(session, "first?", Recorder())
    rec = Recorder()
    stats = await agent.answer(session, "second?", rec)
    assert stats.outcome == "too_long" and "new chat" in rec.visible_text


@pytest.mark.filterwarnings("ignore")
def test_ungrounded_figures_heuristic():
    f = guardrails.ungrounded_figures
    assert f("Texas has 29,145,505 people", queried_this_turn=False, evidence="") == ["29,145,505"]
    assert f("about 29 million", queried_this_turn=False, evidence="") == ["29 million"]
    assert f("Texas has 29,145,505 people", queried_this_turn=False, evidence='"rows": [[29145505]]') == []
    assert f("Texas has 29,145,505 people", queried_this_turn=True, evidence="") == []
    assert f("That is 12.5% of 2019 adults", queried_this_turn=False, evidence="") == []


async def test_cancelled_turn_is_not_committed(executor, settings):
    llm = ScriptedLLM(steps=[text_turn("first")])
    session, agent = Session(id="s"), make_agent(llm, executor, settings)
    await agent.answer(session, "What can you tell me about Texas?", Recorder())
    before = list(session.api_messages)
    llm.steps, llm.turn_delay_s = [text_turn("slow")], 5
    task = asyncio.create_task(agent.answer(session, "Population of Ohio?", Recorder()))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.api_messages == before and len(session.transcript) == 2


# ---- output grounding verifier -------------------------------------------------

from app.llm import GroundingVerdict  # noqa: E402

BAD = GroundingVerdict(supported=False, unsupported_claims=["'31 million' is not in the results"], reason="x")


async def test_verifier_sees_the_answer_and_this_turns_results(executor, settings):
    llm = ScriptedLLM(steps=[tool_turn(("run_sql", {"sql": TX_SQL, "purpose": "Texas population"})), text_turn("Texas: 6,000.")])
    stats = await make_agent(llm, executor, settings).answer(Session(id="s"), "Population of Texas?", Recorder())
    assert stats.verifier == "pass" and not stats.grounding_retry
    payload = llm.verify_inputs[0]
    assert "Texas: 6,000." in payload and "[[6000]]" in payload and "Texas population" in payload


async def test_verifier_failure_triggers_one_retry_with_the_claims(executor, settings):
    llm = ScriptedLLM(
        steps=[
            tool_turn(("run_sql", {"sql": TX_SQL, "purpose": "Texas population"})),
            text_turn("Texas has 31 million people."),
            lambda msgs: text_turn("Texas has 6,000 people.") if "31 million" in msgs[-1]["content"] else text_turn("??"),
        ],
        verdicts=[BAD],
    )
    rec = Recorder()
    stats = await make_agent(llm, executor, settings).answer(Session(id="s"), "Population of Texas?", rec)
    assert stats.grounding_retry and stats.verifier == "pass"
    assert rec.visible_text == "Texas has 6,000 people."  # first draft was cleared
    assert len(llm.verify_inputs) == 2  # the corrected answer is verified too


async def test_second_failure_adds_a_visible_caveat(executor, settings):
    llm = ScriptedLLM(
        steps=[
            tool_turn(("run_sql", {"sql": TX_SQL, "purpose": "x"})),
            text_turn("Texas: 31 million."),
            text_turn("Texas: 31 million."),
        ],
        verdicts=[BAD, BAD],
    )
    rec = Recorder()
    session = Session(id="s")
    stats = await make_agent(llm, executor, settings).answer(session, "Population of Texas?", rec)
    assert stats.verifier == "fail" and "couldn't confirm every figure" in rec.visible_text
    assert "couldn't confirm" in session.transcript[-1][1]


async def test_verifier_error_fails_open(executor, settings):
    llm = ScriptedLLM(
        steps=[tool_turn(("run_sql", {"sql": TX_SQL, "purpose": "x"})), text_turn("Texas: 6,000.")], verdicts=[TimeoutError()]
    )
    rec = Recorder()
    stats = await make_agent(llm, executor, settings).answer(Session(id="s"), "Population of Texas?", rec)
    assert stats.outcome == "ok" and stats.verifier == "error" and rec.visible_text == "Texas: 6,000."


async def test_answers_without_figures_skip_the_verifier(executor, settings):
    llm = ScriptedLLM(steps=[text_turn("Did you mean Washington State or a Washington County?")])
    stats = await make_agent(llm, executor, settings).answer(Session(id="s"), "Population of Washington?", Recorder())
    assert stats.verifier == "not_run" and llm.verify_inputs == []


async def test_no_retry_when_too_little_time_is_left(executor, settings):
    # A retry needs > 20 s left; with a 19 s budget the verifier still runs (needs 8 s) but the
    # failure is reported as a caveat instead of a retry.
    settings.request_deadline_s = 19
    llm = ScriptedLLM(
        steps=[tool_turn(("run_sql", {"sql": TX_SQL, "purpose": "x"})), text_turn("Texas: 31 million.")], verdicts=[BAD]
    )
    rec = Recorder()
    stats = await make_agent(llm, executor, settings).answer(Session(id="s"), "Population of Texas?", rec)
    assert not stats.grounding_retry and "couldn't confirm" in rec.visible_text


async def test_verifier_gets_non_sql_tool_results_too(executor, settings):
    """Regression: a live eval flagged '32 Washington Counties' because only SQL results were passed."""
    llm = ScriptedLLM(
        steps=[tool_turn(("resolve_geography", {"place": "Washington"})), text_turn("Washington matched 3 places.")]
    )
    await make_agent(llm, executor, settings).answer(Session(id="s"), "Population of Washington?", Recorder())
    assert "resolve_geography" in llm.verify_inputs[0] and "candidates" in llm.verify_inputs[0]
