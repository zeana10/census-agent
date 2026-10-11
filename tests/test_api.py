"""HTTP layer: auth, SSE framing, degraded mode, rate limiting."""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import Runtime, create_app
from app.warehouse import WarehouseUnavailable

from .fakes import ScriptedLLM, text_turn


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for chunk in body.strip().split("\n\n"):
        if chunk.startswith(":"):
            continue
        lines = dict(line.split(": ", 1) for line in chunk.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


@pytest.fixture
def make_client(settings, warehouse):
    def _make(llm=None, **overrides):
        for k, v in overrides.items():
            setattr(settings, k, v)
        rt = Runtime(settings, warehouse=warehouse, llm=llm or ScriptedLLM(steps=[text_turn("Hello from the agent.")]))
        return TestClient(create_app(settings, runtime=rt))

    return _make


def test_auth_required_when_configured(make_client):
    with make_client(basic_auth_user="reviewer", basic_auth_password="pw") as c:
        assert c.get("/").status_code == 401
        assert c.post("/api/chat", json={"message": "hi"}).status_code == 401
        assert c.get("/", auth=("reviewer", "wrong")).status_code == 401
        assert c.get("/", auth=("reviewer", "pw")).status_code == 200
        assert c.get("/healthz").status_code == 200  # health check stays open


def test_chat_streams_sse_and_keeps_session(make_client):
    with make_client() as c:
        r = c.post("/api/chat", json={"message": "Population of Texas?"})
        assert r.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(r.text)
        assert events[0][0] == "session"
        sid = events[0][1]["session_id"]
        assert ("delta", {"text": "Hello from the agent."}) in events
        assert events[-1][0] == "done"
        hist = c.get("/api/history", params={"session_id": sid}).json()
        assert [m["role"] for m in hist["messages"]] == ["user", "assistant"]


def test_degraded_mode_when_snowflake_is_down(make_client, warehouse):
    warehouse.fail_with = WarehouseUnavailable("down")
    with make_client() as c:
        assert c.get("/healthz").json()["status"] == "degraded"
        events = parse_sse(c.post("/api/chat", json={"message": "Population of Texas?"}).text)
        assert "trouble connecting" in "".join(d.get("text", "") for e, d in events if e == "delta")
        assert events[-1][0] == "done" and events[-1][1]["outcome"] == "degraded"


def _chat(c, session_id=None, ip=None):
    headers = {"x-forwarded-for": ip} if ip else {}
    events = parse_sse(c.post("/api/chat", json={"session_id": session_id, "message": "q?"}, headers=headers).text)
    return events[0][1]["session_id"], events[-1][1]


def test_per_session_limit(make_client):
    llm = ScriptedLLM(steps=[text_turn("ok")] * 3)
    with make_client(llm=llm, rate_limit_per_session_per_minute=2) as c:
        sid, _ = _chat(c)
        outcomes = [_chat(c, sid)[1] for _ in range(2)]
        other = _chat(c)[1]  # a different session is unaffected
    assert outcomes[0]["outcome"] == "ok"
    assert outcomes[1] == {**outcomes[1], "outcome": "rate_limited", "tier": "session"}
    assert other["outcome"] == "ok"


def test_reviewers_behind_one_office_ip_each_get_their_own_allowance(make_client):
    """The bug this design fixes: a per-IP-only limit of 12/min shared by a whole office."""
    llm = ScriptedLLM(steps=[text_turn("ok")] * 30)
    with make_client(llm=llm, rate_limit_per_session_per_minute=3, rate_limit_per_ip_per_minute=60, trusted_proxy_hops=1) as c:
        sessions = [_chat(c, ip="198.51.100.7")[0] for _ in range(5)]  # 5 reviewers, 1 message each
        follow_ups = [_chat(c, sid, ip="198.51.100.7")[1]["outcome"] for sid in sessions]  # each sends another
    assert follow_ups == ["ok"] * 5


def test_ip_cap_stops_session_rotation(make_client):
    llm = ScriptedLLM(steps=[text_turn("ok")] * 10)
    with make_client(llm=llm, rate_limit_per_session_per_minute=5, rate_limit_per_ip_per_minute=3, trusted_proxy_hops=1) as c:
        outcomes = [_chat(c, ip="203.0.113.9")[1] for _ in range(4)]  # new session every time
    assert [o["outcome"] for o in outcomes] == ["ok", "ok", "ok", "rate_limited"]
    assert outcomes[-1]["tier"] == "ip"


def test_global_cap(make_client):
    llm = ScriptedLLM(steps=[text_turn("ok")] * 10)
    with make_client(llm=llm, rate_limit_global_per_minute=2, trusted_proxy_hops=1) as c:
        outcomes = [_chat(c, ip=f"192.0.2.{i}")[1] for i in range(3)]  # different sessions and IPs
    assert outcomes[-1]["outcome"] == "rate_limited" and outcomes[-1]["tier"] == "global"


def test_rejected_request_does_not_consume_other_tiers():
    from app.main import TieredLimiter

    lim = TieredLimiter(per_session=1, per_ip=10, global_=10)
    assert lim.check(session="a", ip="1") is None
    for _ in range(5):
        assert lim.check(session="a", ip="1") == "session"  # refused...
    assert lim.tiers["ip"].hits["1"].__len__() == 1  # ...and not counted against the IP or global tiers
    assert len(lim.tiers["global"].hits["*"]) == 1


def test_reset_starts_fresh_session(make_client):
    with make_client() as c:
        sid = parse_sse(c.post("/api/chat", json={"message": "q?"}).text)[0][1]["session_id"]
        new = c.post("/api/reset", json={"session_id": sid}).json()["session_id"]
        assert new != sid
        assert c.get("/api/history", params={"session_id": sid}).json()["messages"] == []


def test_bogus_session_ids_are_replaced(make_client):
    with make_client() as c:
        events = parse_sse(c.post("/api/chat", json={"session_id": "../../etc", "message": "q?"}).text)
        sid = events[0][1]["session_id"]
        assert len(sid) == 32 and sid != "../../etc"


def test_request_id_in_header_done_event_and_logs(make_client, capsys):
    with make_client() as c:
        r = c.post("/api/chat", json={"message": "Population of Texas?"})
    rid = r.headers["x-request-id"]
    assert parse_sse(r.text)[-1][1]["request_id"] == rid
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    turn = next(line for line in lines if line["msg"] == "turn complete")
    assert turn["request_id"] == rid and turn["outcome"] == "ok" and "elapsed_s" in turn
