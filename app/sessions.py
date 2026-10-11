"""Conversation state.

In-memory, per process. That keeps the demo dependency-free but means a restart
drops conversations and the app must run as a single instance. The interface
is small on purpose so a Redis/Postgres-backed store can replace it (see REFLECTION.md).

History is append-only: we never edit or trim earlier turns. Thinking blocks
returned by the model are only valid with the exact history that produced them,
so a long conversation hits a turn limit and the user starts a new chat.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class Session:
    id: str
    # Full API-format history (assistant content blocks, tool results), sent back to the model.
    api_messages: list[dict] = field(default_factory=list)
    # Plain (role, text) pairs. Used by the guardrail and to re-render the chat.
    transcript: list[tuple[str, str]] = field(default_factory=list)
    last_used: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def user_turns(self) -> int:
        return sum(1 for r, _ in self.transcript if r == "user")

    def commit(self, messages: list[dict], question: str, answer: str) -> None:
        self.api_messages = messages
        self.transcript += [("user", question), ("assistant", answer)]
        self.last_used = time.monotonic()


class SessionStore:
    def __init__(self, ttl_s: int, max_sessions: int = 5000):
        self._ttl = ttl_s
        self._max = max_sessions
        self._sessions: dict[str, Session] = {}

    def get_or_create(self, session_id: str | None) -> Session:
        self._evict()
        if session_id and session_id in self._sessions:
            s = self._sessions[session_id]
            s.last_used = time.monotonic()
            return s
        sid = session_id if session_id is not None and _valid_id(session_id) else uuid.uuid4().hex
        s = self._sessions[sid] = Session(id=sid)
        return s

    def reset(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def _evict(self) -> None:
        now = time.monotonic()
        for sid in [k for k, s in self._sessions.items() if now - s.last_used > self._ttl and not s.lock.locked()]:
            del self._sessions[sid]
        if len(self._sessions) > self._max:
            for sid, _ in sorted(self._sessions.items(), key=lambda kv: kv[1].last_used)[: len(self._sessions) - self._max]:
                del self._sessions[sid]


def _valid_id(sid: str) -> bool:
    return len(sid) == 32 and all(c in "0123456789abcdef" for c in sid)
