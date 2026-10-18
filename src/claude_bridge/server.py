"""aiohttp application: OpenAI-compatible endpoints in front of `claude -p`."""

from __future__ import annotations

import asyncio
import hmac
import logging
import subprocess
import time
from collections import Counter
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

from . import __version__
from .config import Config
from .openai_compat import chat_completion, error_body, parse_chat_request
from .quota import QuotaGuard
from .runner import BridgeError, run_claude

log = logging.getLogger("claude_bridge.server")

CONFIG = web.AppKey("config", Config)
STATE = web.AppKey("state", "BridgeState")


class BridgeState:
    def __init__(self, cfg: Config, binary: str, token: str) -> None:
        self.cfg = cfg
        self.binary = binary
        self.token = token.encode("utf-8")
        self.quota = QuotaGuard(cfg.quota_limits, cfg.state_file)
        self.semaphore = asyncio.Semaphore(cfg.max_concurrency)
        self.active = 0
        self.queued = 0
        self.started_at = time.time()
        self.outcomes: Counter[str] = Counter()
        self.claude_code_version = _claude_version(binary)
        self.last_error: Optional[dict[str, Any]] = None


def _claude_version(binary: str) -> Optional[str]:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    # "2.1.302 (Claude Code)" -> "2.1.302", the form stream-json init reports.
    return out.stdout.strip().split(" ")[0] or None


def _json_error(error: BridgeError) -> web.Response:
    headers = {}
    if error.retry_after is not None:
        headers["Retry-After"] = str(error.retry_after)
    return web.json_response(error_body(error), status=error.status, headers=headers)


@web.middleware
async def auth_middleware(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
    if request.path.startswith("/v1/"):
        state = request.app[STATE]
        header = request.headers.get("Authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(presented.strip().encode("utf-8"), state.token):
            return _json_error(BridgeError(401, "unauthorized", "missing or invalid bearer token"))
    return await handler(request)


async def chat_completions(request: web.Request) -> web.Response:
    state = request.app[STATE]
    try:
        body = await request.json()
    except ValueError:
        return _json_error(BridgeError(400, "invalid_request", "request body is not valid JSON"))

    try:
        req, ignored = parse_chat_request(body, state.cfg)
    except BridgeError as error:
        return _json_error(error)

    decision = state.quota.check()
    if decision.blocked:
        state.outcomes["quota_blocked"] += 1
        return _json_error(BridgeError(
            429, "quota_guard", f"bridge quota guard: {decision.reason}", retry_after=decision.retry_after,
        ))

    if state.semaphore.locked() and state.queued >= state.cfg.max_queue:
        state.outcomes["queue_full"] += 1
        return _json_error(BridgeError(503, "queue_full", "bridge queue is full", retry_after=5))

    queued_at = time.monotonic()
    state.queued += 1
    try:
        await state.semaphore.acquire()
    finally:
        state.queued -= 1
    queue_wait_ms = int((time.monotonic() - queued_at) * 1000)

    state.active += 1
    try:
        result = await run_claude(state.cfg, state.binary, req, on_rate_limit=state.quota.update)
    except BridgeError as error:
        state.outcomes[error.code] += 1
        state.last_error = {"at": time.time(), "status": error.status, "code": error.code, "message": error.message[:300]}
        log.warning(
            "call failed model=%s effort=%s status=%s code=%s queue_ms=%d msg=%s",
            req.model, req.effort, error.status, error.code, queue_wait_ms, error.message[:200],
        )
        return _json_error(error)
    finally:
        state.active -= 1
        state.semaphore.release()

    state.outcomes["ok"] += 1
    if result.claude_code_version:
        state.claude_code_version = result.claude_code_version
    response = chat_completion(
        req, result, ignored_params=ignored, queue_wait_ms=queue_wait_ms, quota=state.quota.snapshot(),
    )
    windows = response["x_claude"]["quota"]["windows"]
    log.info(
        "call ok model=%s effort=%s schema=%s wall_ms=%d api_ms=%s queue_ms=%d in=%d out=%d cost_eq=%s 5h=%s 7d=%s",
        req.model, req.effort, req.schema is not None, result.wall_ms, result.duration_api_ms, queue_wait_ms,
        response["usage"]["prompt_tokens"], response["usage"]["completion_tokens"], result.cost_usd,
        (windows.get("five_hour") or {}).get("utilization_percent"),
        (windows.get("seven_day") or {}).get("utilization_percent"),
    )
    return web.json_response(response)


async def list_models(request: web.Request) -> web.Response:
    state = request.app[STATE]
    created = int(state.started_at)
    return web.json_response({
        "object": "list",
        "data": [
            {"id": model, "object": "model", "created": created, "owned_by": "anthropic"}
            for model in state.cfg.models
        ],
    })


async def health(request: web.Request) -> web.Response:
    state = request.app[STATE]
    quota = state.quota.snapshot()
    return web.json_response({
        "status": "quota_blocked" if quota["blocked"] else "ok",
        "bridge_version": __version__,
        "claude_code_version": state.claude_code_version,
        "auth_mode": "api_key" if state.cfg.uses_api_key else (
            "oauth_token" if state.cfg.oauth_token_file else "claude_login"
        ),
        "active_calls": state.active,
        "queued_calls": state.queued,
        "max_concurrency": state.cfg.max_concurrency,
        "max_queue": state.cfg.max_queue,
        "uptime_s": int(time.time() - state.started_at),
        "outcomes": dict(state.outcomes),
        "last_error": state.last_error,
        "quota": quota,
    })


def create_app(cfg: Config, *, binary: Optional[str] = None, token: Optional[str] = None) -> web.Application:
    app = web.Application(middlewares=[auth_middleware], client_max_size=8 * 1024 * 1024)
    app[CONFIG] = cfg
    app[STATE] = BridgeState(cfg, binary or cfg.resolved_binary(), token or cfg.read_token())
    app.router.add_post("/v1/chat/completions", chat_completions)
    app.router.add_get("/v1/models", list_models)
    app.router.add_get("/health", health)
    return app


async def serve(cfg: Config) -> None:
    app = create_app(cfg)
    # A client that hangs up (e.g. the analysis budget expired) cancels the
    # handler, which kills the claude process instead of finishing unseen work.
    runner = web.AppRunner(app, handler_cancellation=True, access_log=None)
    await runner.setup()
    for host in cfg.hosts:
        await web.TCPSite(runner, host, cfg.port).start()
        log.info("listening on http://%s:%d", host, cfg.port)
    state = app[STATE]
    log.info(
        "claude-bridge %s ready (claude %s, models=%s, concurrency=%d, quota limits=%s)",
        __version__, state.claude_code_version, ",".join(cfg.models), cfg.max_concurrency, cfg.quota_limits,
    )
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
