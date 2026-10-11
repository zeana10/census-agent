"""FastAPI app: static chat UI + a streaming (Server-Sent Events) chat endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import export, logging_setup
from .agent import MSG_WAREHOUSE_DOWN, Agent
from .catalog import load_or_build
from .config import Settings, get_settings
from .geography import Geography
from .llm import LLM, AnthropicLLM
from .prompts import build_agent_system
from .sessions import ResultTable, SessionStore, valid_client_id
from .store import StateStore
from .tools import ToolExecutor
from .warehouse import SnowflakeWarehouse, Warehouse

log = logging.getLogger("census_agent")
STATIC = Path(__file__).parent / "static"


class Runtime:
    """Holds the wired-up agent. If Snowflake is unreachable at boot we start in
    degraded mode and retry the catalog load on later requests (with backoff)
    instead of crash-looping the container."""

    def __init__(
        self,
        settings: Settings,
        *,
        warehouse: Warehouse | None = None,
        llm: LLM | None = None,
        store: StateStore | None = None,
    ):
        self.s = settings
        self.store = store
        self.wh = warehouse or SnowflakeWarehouse(settings)
        self.llm = llm or AnthropicLLM(settings)
        self.agent: Agent | None = None
        self.last_error: str | None = None
        self._next_try = 0.0
        self._lock = asyncio.Lock()

    async def ensure_ready(self) -> Agent | None:
        if self.agent is not None:
            return self.agent
        async with self._lock:
            if self.agent is not None or time.monotonic() < self._next_try:
                return self.agent
            try:
                catalog = await asyncio.to_thread(
                    load_or_build,
                    self.wh,
                    cache_path=self.s.catalog_cache_path,
                    database=self.s.snowflake_database,
                    schema=self.s.snowflake_schema,
                    years=self.s.years,
                )
                tools = ToolExecutor(
                    catalog,
                    Geography(catalog),
                    self.wh,
                    max_rows=self.s.max_result_rows,
                    table_max_rows=self.s.table_max_rows,
                    store=self.store,
                    memory_limit=self.s.memories_per_client,
                )
                system = build_agent_system(catalog, max_rows=self.s.max_result_rows, table_max_rows=self.s.table_max_rows)
                self.agent = Agent(self.llm, tools, system, self.s)
                self.last_error = None
                log.info("agent ready: %d tables, %d fields", len(catalog.tables), len(catalog.fields))
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                self._next_try = time.monotonic() + 30
                log.exception("catalog load failed; running degraded")
            return self.agent


# Everything the page needs is served by this app: no CDNs, no inline scripts or styles, no framing.
# A Content-Security-Policy this strict means injected markup can't run code even if sanitising failed.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
        "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Strict-Transport-Security": "max-age=31536000",
}


class ChatRequest(BaseModel):
    session_id: str | None = None
    client_id: str | None = None  # random id the browser keeps; scopes conversations and long-term memory
    message: str = Field(max_length=20_000)


class ResetRequest(BaseModel):
    session_id: str = ""
    client_id: str | None = None


class RateLimiter:
    """Sliding one-minute window per key. In-process only (see REFLECTION.md)."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self._last_sweep = time.monotonic()

    def _window(self, key: str, now: float, window_s: float = 60) -> deque[float]:
        if now - self._last_sweep > 300:  # drop idle keys so memory doesn't grow without bound
            self.hits = defaultdict(deque, {k: q for k, q in self.hits.items() if q and now - q[-1] <= 3600})
            self._last_sweep = now
        q = self.hits[key]
        while q and now - q[0] > window_s:
            q.popleft()
        return q

    def would_allow(self, key: str, now: float, window_s: float = 60) -> bool:
        return len(self._window(key, now, window_s)) < self.per_minute

    def record(self, key: str, now: float) -> None:
        self._window(key, now).append(now)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        if not self.would_allow(key, now):
            return False
        self.record(key, now)
        return True


class TieredLimiter:
    """Three limits that must all pass:

    * per session: fairness between users. Several reviewers behind one office IP each
      get their own allowance, which a per-IP-only limit would not give them;
    * per IP, set much higher: stops one client inventing new session ids to get around
      the per-session limit;
    * global: a cost ceiling on model and warehouse spend if all else fails.

    A message is counted against every tier only when all tiers allow it, so a request
    rejected by one tier doesn't use up another tier's allowance.
    """

    def __init__(self, *, per_session: int, per_ip: int, global_: int):
        self.tiers = {"session": RateLimiter(per_session), "ip": RateLimiter(per_ip), "global": RateLimiter(global_)}

    def check(self, *, session: str, ip: str) -> str | None:
        """None if allowed (and recorded); otherwise the name of the tier that refused."""
        now = time.monotonic()
        keys = {"session": session, "ip": ip, "global": "*"}
        for name, limiter in self.tiers.items():
            if not limiter.would_allow(keys[name], now):
                return name
        for name, limiter in self.tiers.items():
            limiter.record(keys[name], now)
        return None


def client_ip(request: Request, *, trusted_hops: int) -> str:
    """The caller's IP, robust to spoofed X-Forwarded-For headers.

    Each proxy appends the address it received the request from, so the client
    controls everything to the LEFT of what our own proxies added. With N trusted
    proxies in front of the app, the real client is the N-th entry from the right.
    Anything further left is attacker-controlled and ignored.
    """
    peer = request.client.host if request.client else "unknown"
    if trusted_hops <= 0:
        return peer
    hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    if len(hops) >= trusted_hops:
        return hops[-trusted_hops]
    return hops[0] if hops else peer


def create_app(settings: Settings | None = None, *, runtime: Runtime | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging_setup.configure(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store, app.state.state_mode = _open_store(settings.state_db_path)
        purged = store.purge(older_than_s=settings.conversation_ttl_days * 86_400)
        log.info("state store ready", extra={"mode": app.state.state_mode, "purged_conversations": purged})
        app.state.store = store
        app.state.runtime = runtime or Runtime(settings, store=store)
        if app.state.runtime.store is None:
            app.state.runtime.store = store
        app.state.sessions = SessionStore(ttl_s=settings.session_ttl_s, store=store)
        app.state.limiter = TieredLimiter(
            per_session=settings.rate_limit_per_session_per_minute,
            per_ip=settings.rate_limit_per_ip_per_minute,
            global_=settings.rate_limit_global_per_minute,
        )
        # Load the catalog in the background so the server accepts connections (and passes
        # health checks) immediately, even if Snowflake is slow or down. Chat requests that
        # arrive first wait on the same lock inside ensure_ready().
        warmup = asyncio.create_task(app.state.runtime.ensure_ready())
        yield
        warmup.cancel()

    app = FastAPI(title="Census Assistant", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    basic = HTTPBasic(auto_error=False)

    login_failures = RateLimiter(settings.login_failures_per_window)

    def require_auth(request: Request, creds: HTTPBasicCredentials | None = Depends(basic)) -> None:
        if not settings.basic_auth_user:
            return
        if creds is None:  # the browser's first, credential-less request: just ask for the login
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required", headers={"WWW-Authenticate": "Basic"})
        ip = client_ip(request, trusted_hops=settings.trusted_proxy_hops)
        now = time.monotonic()
        # Checked BEFORE the password, so a locked-out guesser learns nothing even if they hit the right one.
        if not login_failures.would_allow(ip, now, window_s=settings.login_lockout_s):
            log.warning("login locked out", extra={"tier": "login"})
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many failed sign-in attempts. Try again in a few minutes.",
                headers={"Retry-After": str(settings.login_lockout_s)},
            )
        ok = secrets.compare_digest(creds.username.encode(), settings.basic_auth_user.encode()) and secrets.compare_digest(
            creds.password.encode(), settings.basic_auth_password.encode()
        )
        if not ok:
            login_failures.record(ip, now)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required", headers={"WWW-Authenticate": "Basic"})

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        # Public and minimal: platforms poll it without credentials. Always 200 so the container is
        # kept up; "degraded" means the database is unreachable (the UI explains that to users).
        ready = request.app.state.runtime.agent is not None
        return JSONResponse({"status": "ok" if ready else "degraded"})

    @app.get("/api/status", dependencies=[Depends(require_auth)])
    async def status_detail(request: Request) -> JSONResponse:
        """Operational detail for whoever holds the login: why it's degraded, where state is kept,
        and the proxy header shape used to choose TRUSTED_PROXY_HOPS on a new host."""
        rt: Runtime = request.app.state.runtime
        ready = rt.agent is not None
        xff = [h for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        client_ip_headers = sorted(h for h in ("true-client-ip", "cf-connecting-ip", "x-real-ip") if h in request.headers)
        return JSONResponse(
            {
                "status": "ok" if ready else "degraded",
                "detail": None if ready else "census catalog not loaded",
                # "file" + on_mounted_volume=true: conversations and memories survive deploys.
                # on_mounted_volume=false: the container's own filesystem, wiped on every deploy.
                # "memory": the state file couldn't be opened (the app keeps working either way).
                "state": {
                    "store": request.app.state.state_mode,
                    "path": settings.state_db_path,
                    "on_mounted_volume": _on_mount(settings.state_db_path),
                },
                "proxy": {
                    # Counts and header names only, never addresses: N entries means N proxies append.
                    "forwarded_for_entries": len(xff),
                    "client_ip_headers": client_ip_headers,
                    "trusted_proxy_hops": settings.trusted_proxy_hops,
                },
            }
        )

    @app.get("/", dependencies=[Depends(require_auth)])
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.post("/api/session", dependencies=[Depends(require_auth)])
    async def new_session(request: Request) -> dict[str, str]:
        return {"session_id": request.app.state.sessions.get_or_create(None).id}

    @app.get("/api/history", dependencies=[Depends(require_auth)])
    async def history(request: Request, session_id: str, client_id: str | None = None) -> dict[str, Any]:
        sessions: SessionStore = request.app.state.sessions
        s = sessions.owned(session_id, valid_client_id(client_id)) or sessions.get_or_create(None, valid_client_id(client_id))
        messages = []
        for i, (role, text) in enumerate(s.transcript):
            tables = []
            for table_id in s.transcript_tables.get(i, []):
                t = s.get_table(table_id)
                if t is not None:
                    tables.append(_table_event(s.id, table_id, t, request.app.state.runtime.s.table_max_rows))
            messages.append({"role": role, "text": text, "tables": tables})
        return {"session_id": s.id, "messages": messages}

    def _client(client_id: str | None) -> str:
        cid = valid_client_id(client_id)
        if cid is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "client_id must be 32 lowercase hex characters")
        return cid

    @app.get("/api/memory", dependencies=[Depends(require_auth)])
    async def list_memory(request: Request, client_id: str) -> dict[str, Any]:
        mems = await asyncio.to_thread(request.app.state.store.list_memories, _client(client_id))
        return {"memories": [m.as_dict() for m in mems]}

    @app.delete("/api/memory/{memory_id}", dependencies=[Depends(require_auth)])
    async def forget_memory(request: Request, memory_id: int, client_id: str) -> dict[str, Any]:
        ok = await asyncio.to_thread(request.app.state.store.delete_memory, _client(client_id), memory_id)
        if not ok:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such memory")
        return {"deleted": memory_id}

    @app.delete("/api/memory", dependencies=[Depends(require_auth)])
    async def clear_memory(request: Request, client_id: str) -> dict[str, Any]:
        n = await asyncio.to_thread(request.app.state.store.clear_memories, _client(client_id))
        return {"deleted": n}

    @app.post("/api/chat", dependencies=[Depends(require_auth)])
    async def chat(req: ChatRequest, request: Request) -> Response:
        sessions: SessionStore = request.app.state.sessions
        session = sessions.get_or_create(req.session_id, valid_client_id(req.client_id))
        ip = client_ip(request, trusted_hops=settings.trusted_proxy_hops)
        rid = uuid.uuid4().hex[:12]
        queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()

        async def emit(event: str, data: dict) -> None:
            if event == "done":
                data = {**data, "request_id": rid}  # lets a user quote it in a bug report
            await queue.put((event, data))

        async def run() -> None:
            logging_setup.request_id.set(rid)  # this task's context only
            logging_setup.session_tag.set(session.id[:8])
            try:
                refused = request.app.state.limiter.check(session=session.id, ip=ip)
                if refused is not None:
                    log.warning("rate limited", extra={"tier": refused})
                    text = (
                        "You're sending messages faster than I can answer. Please wait a moment and try again."
                        if refused == "session"
                        else "The assistant is handling a lot of requests right now. Please try again in a minute."
                    )
                    await emit("delta", {"text": text})
                    await emit("done", {"outcome": "rate_limited", "tier": refused})
                    return
                if session.lock.locked():
                    await emit("delta", {"text": "I'm still working on your previous question. One moment."})
                    await emit("done", {"outcome": "busy"})
                    return
                async with session.lock:
                    agent = await request.app.state.runtime.ensure_ready()
                    if agent is None:
                        await emit("delta", {"text": MSG_WAREHOUSE_DOWN})
                        await emit("done", {"outcome": "degraded"})
                        return
                    await agent.answer(session, req.message, emit)
            except Exception:
                log.exception("chat handler failed")
                await emit("replace", {"text": "Something went wrong on my side. Please try again."})
                await emit("done", {"outcome": "internal_error"})
            finally:
                await queue.put(None)

        async def stream() -> AsyncIterator[str]:
            task = asyncio.create_task(run())
            yield _sse("session", {"session_id": session.id})
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=10)
                    except TimeoutError:
                        yield ": keep-alive\n\n"  # stops proxies from closing an idle stream
                        continue
                    if item is None:
                        break
                    yield _sse(*item)
            finally:
                if not task.done():
                    task.cancel()  # client went away; the partial turn is discarded, never committed

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-ID": rid},
        )

    @app.get("/api/tables/{session_id}/{table_id}.{fmt}", dependencies=[Depends(require_auth)])
    async def download_table(request: Request, session_id: str, table_id: str, fmt: str) -> Response:
        session = request.app.state.sessions.get(session_id)
        table = session.get_table(table_id) if session else None
        if table is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "This table has expired. Ask the question again to get a fresh download."
            )
        if fmt == "csv":
            body, media = export.to_csv(table), "text/csv; charset=utf-8"
        elif fmt == "xlsx":
            body = await asyncio.to_thread(export.to_xlsx, table)
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        else:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown format; use .csv or .xlsx")
        return Response(
            body,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{export.filename(table, fmt)}"', "Cache-Control": "no-store"},
        )

    @app.post("/api/reset", dependencies=[Depends(require_auth)])
    async def reset(body: ResetRequest, request: Request) -> dict[str, str]:
        sessions: SessionStore = request.app.state.sessions
        cid = valid_client_id(body.client_id)
        if sessions.owned(body.session_id, cid) is not None:  # can't delete someone else's conversation
            sessions.reset(body.session_id)
        return {"session_id": sessions.get_or_create(None, cid).id}

    return app


def _on_mount(path: str) -> bool:
    if path == ":memory:":
        return False
    p = os.path.dirname(os.path.abspath(path))
    while p and p != os.path.dirname(p):
        if os.path.ismount(p):
            return True
        p = os.path.dirname(p)
    return False  # only "/" itself, which is the container's filesystem


def _open_store(path: str) -> tuple[StateStore, str]:
    """Open the durable store, or fall back to an in-memory one rather than failing to start.
    A missing or read-only disk should cost us persistence, not the whole service."""
    try:
        return StateStore(path), "file"
    except (OSError, sqlite3.Error) as e:
        log.error("state store at %s unavailable (%s); using in-memory state until fixed", path, e)
        return StateStore(":memory:"), "memory"


def _table_event(session_id: str, table_id: str, t: ResultTable, max_rows: int) -> dict[str, Any]:
    return {
        "id": table_id,
        "session_id": session_id,
        "purpose": t.purpose,
        "columns": t.columns,
        "rows": t.rows,
        "truncated": t.truncated,
        "max_rows": max_rows,
    }


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


app = create_app()
