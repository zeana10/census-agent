"""Data access. All SQL that reaches Snowflake goes through `Warehouse.query`.

Defence in depth for model-written SQL:
  1. `sql_guard.validate` (static allow-list check) runs before this module sees the SQL.
  2. The Snowflake role only holds IMPORTED PRIVILEGES on the census share (read-only).
  3. Each session sets STATEMENT_TIMEOUT_IN_SECONDS so a runaway query cannot hang a turn.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import decimal
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .config import Settings

log = logging.getLogger(__name__)


class WarehouseError(Exception):
    """A query failed. `user_safe` is a message we can show the model / the user."""

    def __init__(self, user_safe: str, *, retryable: bool = False):
        super().__init__(user_safe)
        self.user_safe = user_safe
        self.retryable = retryable


class WarehouseUnavailable(WarehouseError):
    """We could not reach Snowflake at all (auth, network, suspended account)."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    elapsed_ms: int


class Warehouse(Protocol):
    async def query(self, sql: str, *, max_rows: int) -> QueryResult: ...

    def query_sync(self, sql: str, *, max_rows: int) -> QueryResult: ...


def _jsonable(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, float) and v.is_integer():
        return int(v)  # the 2020 tables store counts as FLOAT; "28635442.0" reads as a measurement
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


class ConnectionPool:
    """A small blocking pool. Connections are created lazily up to `max_size`;
    callers wait (up to `acquire_timeout_s`) when all are busy. A connection
    that failed is closed and dropped instead of being returned to the pool."""

    def __init__(self, factory: Callable[[], Any], *, max_size: int, acquire_timeout_s: float):
        self._factory = factory
        self._max = max_size
        self._timeout = acquire_timeout_s
        self._idle: list[Any] = []
        self._created = 0
        self._cond = threading.Condition()

    def acquire(self) -> Any:
        deadline = time.monotonic() + self._timeout
        with self._cond:
            while True:
                while self._idle:
                    conn = self._idle.pop()
                    if not _is_closed(conn):
                        return conn
                    self._created -= 1
                if self._created < self._max:
                    self._created += 1
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WarehouseUnavailable(
                        "The census database is busy with other requests. Please try again in a moment.",
                        retryable=True,
                    )
                self._cond.wait(remaining)
        try:
            return self._factory()  # connect outside the lock; it can take seconds
        except BaseException:
            with self._cond:
                self._created -= 1
                self._cond.notify()
            raise

    def release(self, conn: Any, *, broken: bool = False) -> None:
        with self._cond:
            if broken or _is_closed(conn):
                self._created -= 1
                _close_quietly(conn)
            else:
                self._idle.append(conn)
            self._cond.notify()

    @property
    def size(self) -> int:
        return self._created


def _is_closed(conn: Any) -> bool:
    try:
        return bool(conn.is_closed())
    except Exception:
        return True


def _close_quietly(conn: Any) -> None:
    try:
        conn.close()
    except Exception as e:  # already dead; nothing useful to do
        log.debug("ignoring error closing connection: %s", e)


class SnowflakeWarehouse:
    """snowflake-connector-python behind a connection pool. A connection is not
    thread-safe, so each query borrows one exclusively. Concurrent users run their
    queries in parallel up to `snowflake_pool_size`."""

    def __init__(self, settings: Settings):
        self._s = settings
        self.pool = ConnectionPool(self._connect, max_size=settings.snowflake_pool_size, acquire_timeout_s=settings.sql_timeout_s)

    def _connect(self):
        import snowflake.connector
        from snowflake.connector.errors import DatabaseError

        kwargs: dict[str, Any] = dict(
            account=self._s.snowflake_account,
            user=self._s.snowflake_user,
            role=self._s.snowflake_role,
            warehouse=self._s.snowflake_warehouse,
            database=self._s.snowflake_database,
            schema=self._s.snowflake_schema,
            login_timeout=15,
            network_timeout=self._s.sql_timeout_s + 10,
            session_parameters={
                "STATEMENT_TIMEOUT_IN_SECONDS": self._s.sql_timeout_s,
                "QUERY_TAG": "census-agent",
            },
            client_session_keep_alive=True,
        )
        key = _load_private_key(self._s)
        if key is not None:
            kwargs["private_key"] = key
        else:
            kwargs["password"] = self._s.snowflake_password
        try:
            return snowflake.connector.connect(**kwargs)
        except DatabaseError as e:
            log.exception("snowflake connect failed")
            raise WarehouseUnavailable("I'm having trouble connecting to the census database right now.") from e

    def query_sync(self, sql: str, *, max_rows: int) -> QueryResult:
        from snowflake.connector.errors import (
            DatabaseError,
            OperationalError,
            ProgrammingError,
        )

        for attempt in (1, 2):
            conn = self.pool.acquire()
            broken = False
            start = time.monotonic()
            try:
                cur = conn.cursor()
                try:
                    cur.execute(sql)
                    rows = cur.fetchmany(max_rows + 1)
                    cols = [d[0] for d in cur.description or []]
                finally:
                    cur.close()
            except ProgrammingError as e:
                # SQL compilation errors, timeouts. Feed the message back to the model so it can fix the query.
                msg = str(getattr(e, "msg", e)).strip()
                if getattr(e, "errno", None) == 604 or "timeout" in msg.lower():
                    raise WarehouseError(
                        f"The query exceeded the {self._s.sql_timeout_s}s time limit. "
                        "Try aggregating at a coarser level or filtering by state."
                    ) from e
                raise WarehouseError(f"SQL error: {msg[:500]}") from e
            except (OperationalError, DatabaseError) as e:
                # Connection dropped or session expired: discard it, retry once on a fresh one.
                log.warning("snowflake operational error (attempt %s): %s", attempt, e)
                broken = True
                if attempt == 2:
                    raise WarehouseUnavailable(
                        "I'm having trouble connecting to the census database right now.",
                        retryable=True,
                    ) from e
                continue
            finally:
                self.pool.release(conn, broken=broken)
            elapsed = int((time.monotonic() - start) * 1000)
            truncated = len(rows) > max_rows
            return QueryResult(
                columns=cols,
                rows=[[_jsonable(v) for v in r] for r in rows[:max_rows]],
                truncated=truncated,
                elapsed_ms=elapsed,
            )
        raise AssertionError("unreachable")

    async def query(self, sql: str, *, max_rows: int) -> QueryResult:
        return await asyncio.to_thread(self.query_sync, sql, max_rows=max_rows)


def _load_private_key(s: Settings) -> bytes | None:
    pem: bytes | None = None
    if s.snowflake_private_key_pem:
        pem = s.snowflake_private_key_pem.replace("\\n", "\n").encode()
    elif s.snowflake_private_key_path:
        with open(s.snowflake_private_key_path, "rb") as f:
            pem = f.read()
    if pem is None:
        return None
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(pem, password=None)
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
