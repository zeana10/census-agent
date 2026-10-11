"""Connection pool and client-IP handling."""

import threading
import time
from types import SimpleNamespace

import pytest

from app.main import RateLimiter, client_ip
from app.warehouse import ConnectionPool, WarehouseUnavailable


class FakeConn:
    def __init__(self):
        self.closed = False

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True


def test_pool_reuses_connections_and_caps_size():
    made = []
    pool = ConnectionPool(lambda: made.append(FakeConn()) or made[-1], max_size=2, acquire_timeout_s=0.2)
    a, b = pool.acquire(), pool.acquire()
    assert a is not b and pool.size == 2
    with pytest.raises(WarehouseUnavailable, match="busy"):
        pool.acquire()  # both checked out, cap reached
    pool.release(a)
    assert pool.acquire() is a and len(made) == 2


def test_pool_runs_queries_concurrently():
    """The old design serialized every query behind one lock; the pool must not."""
    pool = ConnectionPool(FakeConn, max_size=4, acquire_timeout_s=5)
    active, peak, lock = [0], [0], threading.Lock()

    def work():
        c = pool.acquire()
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.1)
        with lock:
            active[0] -= 1
        pool.release(c)

    threads = [threading.Thread(target=work) for _ in range(4)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 4 and time.monotonic() - start < 0.35


def test_waiter_gets_connection_when_one_is_released():
    pool = ConnectionPool(FakeConn, max_size=1, acquire_timeout_s=2)
    held = pool.acquire()
    threading.Timer(0.1, pool.release, args=(held,)).start()
    assert pool.acquire() is held


def test_broken_and_closed_connections_are_replaced():
    pool = ConnectionPool(FakeConn, max_size=1, acquire_timeout_s=0.2)
    a = pool.acquire()
    pool.release(a, broken=True)
    assert a.closed and pool.size == 0
    b = pool.acquire()
    assert b is not a
    pool.release(b)
    b.closed = True  # e.g. session expired while idle
    c = pool.acquire()
    assert c is not b and pool.size == 1


def test_failed_connect_frees_its_slot():
    calls = []

    def factory():
        calls.append(1)
        if len(calls) == 1:
            raise WarehouseUnavailable("down")
        return FakeConn()

    pool = ConnectionPool(factory, max_size=1, acquire_timeout_s=0.2)
    with pytest.raises(WarehouseUnavailable):
        pool.acquire()
    assert pool.acquire() is not None  # slot was not leaked


def req(xff=None, peer="10.0.0.1"):
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers={"x-forwarded-for": xff} if xff else {})


@pytest.mark.parametrize(
    "xff, hops, expected",
    [
        (None, 1, "10.0.0.1"),  # no proxy header: socket peer
        ("203.0.113.7", 1, "203.0.113.7"),  # one proxy appended the real client
        ("6.6.6.6, 203.0.113.7", 1, "203.0.113.7"),  # client-spoofed entry on the left is ignored
        ("6.6.6.6, 203.0.113.7, 10.1.1.1", 2, "203.0.113.7"),  # two trusted proxies
        ("6.6.6.6, 203.0.113.7", 0, "10.0.0.1"),  # header ignored entirely
    ],
)
def test_client_ip_ignores_spoofed_forwarded_for(xff, hops, expected):
    assert client_ip(req(xff), trusted_hops=hops) == expected


def test_spoofing_does_not_bypass_rate_limit():
    rl = RateLimiter(per_minute=2)
    results = [rl.allow(client_ip(req(f"{i}.{i}.{i}.{i}, 203.0.113.7"), trusted_hops=1)) for i in range(3)]
    assert results == [True, True, False]


class ScriptedConn(FakeConn):
    """Behaves like a snowflake connection; `script` is consumed one item per execute()."""

    def __init__(self, script):
        super().__init__()
        self.script = script

    def cursor(self):
        conn = self

        class Cur:
            description = [("POP",)]

            def execute(self, sql):
                step = conn.script.pop(0)
                if isinstance(step, Exception):
                    raise step
                self._rows = step

            def fetchmany(self, n):
                return self._rows[:n]

            def close(self):
                pass

        return Cur()


def make_wh(monkeypatch, scripts):
    from app.config import Settings
    from app.warehouse import SnowflakeWarehouse

    wh = SnowflakeWarehouse(Settings(_env_file=None, snowflake_pool_size=2, sql_timeout_s=1))
    conns = [ScriptedConn(s) for s in scripts]
    monkeypatch.setattr(wh, "_connect", lambda: conns.pop(0))
    wh.pool._factory = wh._connect
    return wh


def test_dropped_connection_is_replaced_and_query_retried(monkeypatch):
    from snowflake.connector.errors import OperationalError

    wh = make_wh(monkeypatch, [[OperationalError("connection reset")], [[(42,)]]])
    assert wh.query_sync("SELECT 1", max_rows=10).rows == [[42]]
    assert wh.pool.size == 1  # broken one discarded, fresh one kept


def test_sql_errors_are_reported_and_connection_kept(monkeypatch):
    from snowflake.connector.errors import ProgrammingError

    from app.warehouse import WarehouseError

    wh = make_wh(monkeypatch, [[ProgrammingError(msg="invalid identifier 'X'"), [(1,)]]])
    with pytest.raises(WarehouseError, match="invalid identifier"):
        wh.query_sync("SELECT X", max_rows=10)
    assert wh.query_sync("SELECT 1", max_rows=10).rows == [[1]]  # same connection reused
    assert wh.pool.size == 1


def test_persistent_outage_raises_unavailable(monkeypatch):
    from snowflake.connector.errors import OperationalError

    wh = make_wh(monkeypatch, [[OperationalError("down")], [OperationalError("down")]])
    with pytest.raises(WarehouseUnavailable):
        wh.query_sync("SELECT 1", max_rows=10)
    assert wh.pool.size == 0


def test_whole_number_floats_become_ints():
    from decimal import Decimal

    from app.warehouse import _jsonable

    assert _jsonable(28635442.0) == 28635442 and isinstance(_jsonable(28635442.0), int)
    assert _jsonable(12.5) == 12.5
    assert _jsonable(Decimal("7")) == 7 and _jsonable(Decimal("7.25")) == 7.25
