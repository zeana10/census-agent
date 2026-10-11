"""Conversation state (short-term memory).

Each conversation keeps its full API history, a plain transcript for the UI and
the guardrail, and the result tables shown in it. Live sessions sit in memory
(with a per-conversation lock); every committed turn is also written to the
SQLite `StateStore`, so a conversation survives deploys and restarts and is
reloaded on demand.

History is append-only: earlier turns are never edited or trimmed, because the
model's thinking blocks are only valid with the exact history that produced
them. Very long conversations are condensed server-side by the API's
compaction feature instead (see llm.py).

A conversation belongs to the browser that started it (`client_id`). Asking for
it with a different client id starts a fresh conversation instead of exposing
someone else's history.
"""

from __future__ import annotations

import asyncio
import secrets
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Any

from .store import StateStore


@dataclass
class ResultTable:
    """A full query result the user can see and download (the model only sees a capped view)."""

    purpose: str
    sql: str
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool  # True when the result had more than table_max_rows rows
    created: float = field(default_factory=time.time)


@dataclass
class Session:
    id: str
    client_id: str | None = None
    # Full API-format history (assistant content blocks, tool results), sent back to the model.
    api_messages: list[dict] = field(default_factory=list)
    # Plain (role, text) pairs. Used by the guardrail and to re-render the chat.
    transcript: list[tuple[str, str]] = field(default_factory=list)
    # transcript index of an assistant answer -> ids of the tables shown with it
    transcript_tables: dict[int, list[str]] = field(default_factory=dict)
    last_used: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tables: OrderedDict[str, ResultTable] = field(default_factory=OrderedDict)
    store: StateStore | None = field(default=None, repr=False, compare=False)
    tables_keep: int = 20

    def add_table(self, table: ResultTable, *, keep: int) -> str:
        """Store a table and return its unguessable id; only the newest `keep` tables are kept."""
        self.tables_keep = keep
        table_id = secrets.token_urlsafe(12)
        self.tables[table_id] = table
        while len(self.tables) > keep:
            self.tables.popitem(last=False)
        if self.store is not None:
            self.store.save_table(table_id, self.id, asdict(table), keep=keep)
        return table_id

    def get_table(self, table_id: str) -> ResultTable | None:
        if table_id in self.tables:
            return self.tables[table_id]
        if self.store is not None:
            raw = self.store.load_table(self.id, table_id)
            if raw is not None:
                return ResultTable(**raw)
        return None

    @property
    def user_turns(self) -> int:
        return sum(1 for r, _ in self.transcript if r == "user")

    def commit(self, messages: list[dict], question: str, answer: str, *, table_ids: list[str] | None = None) -> None:
        self.api_messages = messages
        self.transcript += [("user", question), ("assistant", answer)]
        if table_ids:
            self.transcript_tables[len(self.transcript) - 1] = list(table_ids)
        self.last_used = time.monotonic()
        self.persist()

    def persist(self) -> None:
        if self.store is None:
            return
        transcript = [
            {"role": r, "text": t, "tables": self.transcript_tables.get(i, [])} for i, (r, t) in enumerate(self.transcript)
        ]
        self.store.save_conversation(self.id, self.client_id, self.api_messages, transcript)


class SessionStore:
    def __init__(self, ttl_s: int, max_sessions: int = 5000, *, store: StateStore | None = None):
        self._ttl = ttl_s
        self._max = max_sessions
        self._sessions: dict[str, Session] = {}
        self.store = store

    def get_or_create(self, session_id: str | None, client_id: str | None = None) -> Session:
        self._evict()
        existing = self.get(session_id) if session_id else None
        if existing is not None and _owned_by(existing, client_id):
            existing.last_used = time.monotonic()
            if client_id and not existing.client_id:
                existing.client_id = client_id
            return existing
        # Unknown id, invalid id, or someone else's conversation: start a new one.
        reuse = session_id is not None and _valid_id(session_id) and existing is None
        sid = session_id if reuse and session_id is not None else uuid.uuid4().hex
        s = self._sessions[sid] = Session(id=sid, client_id=client_id, store=self.store)
        return s

    def get(self, session_id: str | None) -> Session | None:
        """An existing session (from memory, else reloaded from the store), without creating one."""
        if not session_id:
            return None
        if session_id in self._sessions:
            return self._sessions[session_id]
        if self.store is None or not _valid_id(session_id):
            return None
        saved = self.store.load_conversation(session_id)
        if saved is None:
            return None
        s = Session(id=session_id, client_id=saved["client_id"], api_messages=saved["api_messages"], store=self.store)
        for i, entry in enumerate(saved["transcript"]):
            s.transcript.append((entry["role"], entry["text"]))
            if entry.get("tables"):
                s.transcript_tables[i] = list(entry["tables"])
        self._sessions[session_id] = s
        return s

    def owned(self, session_id: str | None, client_id: str | None) -> Session | None:
        s = self.get(session_id)
        return s if s is not None and _owned_by(s, client_id) else None

    def reset(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        if self.store is not None and _valid_id(session_id):
            self.store.delete_conversation(session_id)

    def _evict(self) -> None:
        # Only the in-memory cache is evicted; persisted conversations are reloaded on demand.
        now = time.monotonic()
        for sid in [k for k, s in self._sessions.items() if now - s.last_used > self._ttl and not s.lock.locked()]:
            del self._sessions[sid]
        if len(self._sessions) > self._max:
            for sid, _ in sorted(self._sessions.items(), key=lambda kv: kv[1].last_used)[: len(self._sessions) - self._max]:
                del self._sessions[sid]


def _owned_by(s: Session, client_id: str | None) -> bool:
    return s.client_id is None or client_id is None or s.client_id == client_id


def _valid_id(sid: str) -> bool:
    return len(sid) == 32 and all(c in "0123456789abcdef" for c in sid)


def valid_client_id(cid: str | None) -> str | None:
    """Browser ids are 32 lowercase hex chars; anything else is ignored rather than trusted."""
    return cid if cid is not None and _valid_id(cid) else None
