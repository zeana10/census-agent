"""FastAPI app: static chat UI + a streaming (Server-Sent Events) chat endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
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
from pydantic import BaseModel, Field

from . import logging_setup
from .agent import MSG_WAREHOUSE_DOWN, Agent
from .catalog import load_or_build
from .config import Settings, get_settings
from .geography import Geography
from .llm import LLM, AnthropicLLM
from .prompts import build_agent_system
from .sessions import SessionStore
from .tools import ToolExecutor
from .warehouse import SnowflakeWarehouse, Warehouse

log = logging.getLogger("census_agent")
STATIC = Path(__file__).parent / "static"


class Runtime:
    """Holds the wired-up agent. If Snowflake is unreachable at boot we start in
    degraded mode and retry the catalog load on later requests (with backoff)
    instead of crash-looping the container."""

    def __init__(self, settings: Settings, *, warehouse: Warehouse | None = None, llm: LLM | None = None):
        self.s = settings
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
                tools = ToolExecutor(catalog, Geography(catalog), self.wh, max_rows=self.s.max_result_rows)
                self.agent = Agent(self.llm, tools, build_agent_system(catalog, max_rows=self.s.max_result_rows), self.s)
                self.last_error = None
                log.info("agent ready: %d tables, %d fields", len(catalog.tables), len(catalog.fields))
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                self._next_try = time.monotonic() + 30
                log.exception("catalog load failed; running degraded")
            return self.agent


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str = Field(max_length=20_000)


class ResetRequest(BaseModel):
    session_id: str = ""


class RateLimiter:
    """Sliding one-minute window per key. In-process only (see REFLECTION.md)."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self._last_sweep = time.monotonic()

    def _window(self, key: str, now: float) -> deque[float]:
        if now - self._last_sweep > 300:  # drop idle keys so memory doesn't grow without bound
            self.hits = defaultdict(deque, {k: q for k, q in self.hits.items() if q and now - q[-1] <= 60})
            self._last_sweep = now
        q = self.hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        return q

    def would_allow(self, key: str, now: float) -> bool:
        return len(self._window(key, now)) < self.per_minute

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
        app.state.runtime = runtime or Runtime(settings)
        app.state.sessions = SessionStore(ttl_s=settings.session_ttl_s)
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
    basic = HTTPBasic(auto_error=False)

    def require_auth(creds: HTTPBasicCredentials | None = Depends(basic)) -> None:
        if not settings.basic_auth_user:
            return
        ok = (
            creds is not None
            and secrets.compare_digest(creds.username.encode(), settings.basic_auth_user.encode())
            and secrets.compare_digest(creds.password.encode(), settings.basic_auth_password.encode())
        )
        if not ok:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required", headers={"WWW-Authenticate": "Basic"})

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        # Always 200 so the platform keeps the container up; "degraded" means the UI
        # will explain that the database is unreachable instead of answering.
        rt: Runtime = request.app.state.runtime
        ready = rt.agent is not None
        return JSONResponse({"status": "ok" if ready else "degraded", "detail": None if ready else "census catalog not loaded"})

    @app.get("/", dependencies=[Depends(require_auth)])
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.post("/api/session", dependencies=[Depends(require_auth)])
    async def new_session(request: Request) -> dict[str, str]:
        return {"session_id": request.app.state.sessions.get_or_create(None).id}

    @app.get("/api/history", dependencies=[Depends(require_auth)])
    async def history(request: Request, session_id: str) -> dict[str, Any]:
        s = request.app.state.sessions.get_or_create(session_id)
        return {"session_id": s.id, "messages": [{"role": r, "text": t} for r, t in s.transcript]}

    @app.post("/api/chat", dependencies=[Depends(require_auth)])
    async def chat(req: ChatRequest, request: Request) -> Response:
        sessions: SessionStore = request.app.state.sessions
        session = sessions.get_or_create(req.session_id)
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

    @app.post("/api/reset", dependencies=[Depends(require_auth)])
    async def reset(body: ResetRequest, request: Request) -> dict[str, str]:
        request.app.state.sessions.reset(body.session_id)
        return {"session_id": request.app.state.sessions.get_or_create(None).id}

    return app


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


app = create_app()
