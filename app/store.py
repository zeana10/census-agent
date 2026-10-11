"""Durable state: conversations (short-term memory), result tables, and per-browser
long-term memories, in one SQLite file.

SQLite fits this app: one instance, low write volume (one write per turn), and on
Render the file lives on a persistent disk, so state survives deploys and restarts.
The class is small on purpose; swapping in Postgres or Redis means reimplementing
these methods, not touching the agent.

Writes are synchronous and take a few milliseconds. A per-process lock serialises
them; WAL mode lets reads proceed alongside.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MEMORY_KINDS = ("preference", "focus", "note")
MEMORY_MAX_CHARS = 200

# Memories are shown to the model on every future turn, so they're a place an attacker
# would try to plant instructions ("memory poisoning"). They're also personal data.
# Reject both at write time; the agent is told memories are data, never instructions.
_INSTRUCTION_LIKE = re.compile(
    r"\b(ignore|disregard|override|bypass|jailbreak|system prompt|instructions?|rules?|"
    r"you (must|should|are now|will)|act as|pretend|from now on always|do not (check|verify)|"
    r"reveal|password|api key|token)\b",
    re.I,
)
_CODE_LIKE = re.compile(r"(\bselect\b.+\bfrom\b|\bdrop\s+table\b|<\s*script|```|\{\{|\$\{)", re.I | re.S)
_PERSONAL = re.compile(
    r"([\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d\s().-]{8,}\d|\b\d{3}-\d{2}-\d{4}\b)",  # email, phone, SSN-shaped
)


class MemoryRejected(ValueError):
    """The memory isn't something we'll store; the message is safe to show the model."""


@dataclass
class Memory:
    id: int
    kind: str
    text: str
    created: float

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "text": self.text, "created": self.created}


def validate_memory(kind: str, text: str) -> str:
    """Normalised memory text, or MemoryRejected explaining why not."""
    text = " ".join((text or "").split())
    if kind not in MEMORY_KINDS:
        raise MemoryRejected(f"kind must be one of {', '.join(MEMORY_KINDS)}")
    if not 3 <= len(text) <= MEMORY_MAX_CHARS:
        raise MemoryRejected(f"a memory must be 3 to {MEMORY_MAX_CHARS} characters")
    if _INSTRUCTION_LIKE.search(text) or _CODE_LIKE.search(text):
        raise MemoryRejected(
            "memories can only hold the user's preferences and interests, not instructions about how the assistant "
            "behaves, its rules, or code"
        )
    if _PERSONAL.search(text):
        raise MemoryRejected("don't store contact details or identifiers (emails, phone numbers, ID numbers)")
    return text


class StateStore:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    client_id TEXT,
                    updated REAL NOT NULL,
                    api_messages TEXT NOT NULL,
                    transcript TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS result_tables (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    created REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS result_tables_conv ON result_tables(conversation_id);
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS memories_client ON memories(client_id);
                """
            )

    # ---- conversations ---------------------------------------------------------
    def save_conversation(self, conv_id: str, client_id: str | None, api_messages: list, transcript: list) -> None:
        payload = json.dumps([_message_to_json(m) for m in api_messages])
        with self._lock:
            self._db.execute(
                "INSERT INTO conversations (id, client_id, updated, api_messages, transcript) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET client_id=excluded.client_id, updated=excluded.updated, "
                "api_messages=excluded.api_messages, transcript=excluded.transcript",
                (conv_id, client_id, time.time(), payload, json.dumps(transcript)),
            )

    def load_conversation(self, conv_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        if row is None:
            return None
        return {
            "client_id": row["client_id"],
            "api_messages": json.loads(row["api_messages"]),
            "transcript": json.loads(row["transcript"]),
        }

    def delete_conversation(self, conv_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
            self._db.execute("DELETE FROM result_tables WHERE conversation_id = ?", (conv_id,))

    # ---- result tables ---------------------------------------------------------
    def save_table(self, table_id: str, conv_id: str, table: dict[str, Any], *, keep: int) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO result_tables (id, conversation_id, created, payload) VALUES (?, ?, ?, ?)",
                (table_id, conv_id, table.get("created", time.time()), json.dumps(table, default=str)),
            )
            # Keep only the newest `keep` tables per conversation.
            self._db.execute(
                "DELETE FROM result_tables WHERE conversation_id = ? AND id NOT IN ("
                "SELECT id FROM result_tables WHERE conversation_id = ? ORDER BY created DESC LIMIT ?)",
                (conv_id, conv_id, keep),
            )

    def load_table(self, conv_id: str, table_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM result_tables WHERE id = ? AND conversation_id = ?", (table_id, conv_id)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    # ---- long-term memory --------------------------------------------------------
    def list_memories(self, client_id: str) -> list[Memory]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, kind, text, created FROM memories WHERE client_id = ? ORDER BY id", (client_id,)
            ).fetchall()
        return [Memory(r["id"], r["kind"], r["text"], r["created"]) for r in rows]

    def add_memory(self, client_id: str, kind: str, text: str, *, limit: int) -> Memory:
        text = validate_memory(kind, text)
        with self._lock:
            existing = self._db.execute(
                "SELECT id, kind, text, created FROM memories WHERE client_id = ? AND lower(text) = lower(?)",
                (client_id, text),
            ).fetchone()
            if existing:
                return Memory(existing["id"], existing["kind"], existing["text"], existing["created"])
            (count,) = self._db.execute("SELECT COUNT(*) FROM memories WHERE client_id = ?", (client_id,)).fetchone()
            if count >= limit:
                raise MemoryRejected(f"this user already has {limit} memories; ask which one to forget first")
            now = time.time()
            cur = self._db.execute(
                "INSERT INTO memories (client_id, kind, text, created) VALUES (?, ?, ?, ?)", (client_id, kind, text, now)
            )
            return Memory(int(cur.lastrowid or 0), kind, text, now)

    def delete_memory(self, client_id: str, memory_id: int) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM memories WHERE client_id = ? AND id = ?", (client_id, memory_id))
        return cur.rowcount > 0

    def clear_memories(self, client_id: str) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM memories WHERE client_id = ?", (client_id,))
        return cur.rowcount

    # ---- housekeeping ----------------------------------------------------------
    def purge(self, *, older_than_s: float) -> int:
        """Drop conversations (and their tables) not touched for `older_than_s`. Memories are kept."""
        cutoff = time.time() - older_than_s
        with self._lock:
            ids = [r["id"] for r in self._db.execute("SELECT id FROM conversations WHERE updated < ?", (cutoff,))]
            for conv_id in ids:
                self._db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
                self._db.execute("DELETE FROM result_tables WHERE conversation_id = ?", (conv_id,))
        return len(ids)


def _message_to_json(m: dict) -> dict:
    """API messages hold SDK block objects; store them as the plain dicts the API accepts back."""
    content = m.get("content")
    if isinstance(content, list):
        content = [_block_to_json(b) for b in content]
    return {**m, "content": content}


def _block_to_json(b: Any) -> Any:
    if isinstance(b, dict):
        return b
    dump = getattr(b, "model_dump", None)
    if dump is not None:
        return dump(mode="json", exclude_none=True)
    if hasattr(b, "__dict__"):  # simple objects, e.g. test doubles
        return {k: v for k, v in vars(b).items() if v is not None}
    return {"type": "text", "text": str(b)}
