"""Run one headless `claude -p` call and turn its event stream into a result.

Each call is a fresh, throwaway Claude Code process:

* no tools (`--tools ""`), no user/project settings or hooks
  (`--setting-sources ""`), no MCP servers (`--strict-mcp-config`), no slash
  commands, and no session written to disk (`--no-session-persistence`);
* an empty temporary working directory, so there is no project to read;
* the prompt on stdin, the instructions as the whole system prompt;
* a schema, when given, passed to `--json-schema`, so Claude Code validates the
  answer and returns it as `structured_output`.

The process runs in its own session/process group; a timeout or a cancelled
request kills the whole group.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import Config

log = logging.getLogger("claude_bridge.runner")

# A stream-json line carries a whole message; the default 64 KiB reader limit
# is far too small for long answers.
_STREAM_LIMIT = 16 * 1024 * 1024
_STDERR_TAIL = 4000
_KILL_GRACE_SECONDS = 2.0

_OVERLOAD_STATUSES = {500, 502, 503, 504, 529}
_AUTH_STATUSES = {401, 403}
_AUTH_MARKERS = ("/login", "not logged in", "invalid api key", "oauth token", "authentication")
_USAGE_LIMIT_RE = re.compile(r"usage limit reached\|(\d{9,})", re.IGNORECASE)


class BridgeError(Exception):
    """A failed call, already mapped onto the HTTP status the bridge returns.

    429 + Retry-After  quota/rate limit (Claude's or the bridge's own guard)
    503                Claude/Anthropic overloaded, or the bridge queue is full
    504                the call exceeded the bridge timeout
    424                Claude Code is not logged in / the credential was refused
                       (permanent: retrying cannot help, an operator must act)
    502                Claude Code crashed, or returned no schema-valid answer
    400                the request itself was rejected (e.g. prompt too long)
    """

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retry_after: Optional[int] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.details = details or {}


@dataclass
class ClaudeRequest:
    system_prompt: str
    prompt: str
    model: str
    effort: Optional[str] = None
    schema: Optional[dict[str, Any]] = None


@dataclass
class ClaudeResult:
    content: str
    structured_output: Any
    finish_reason: str
    usage: dict[str, Any]
    cost_usd: Optional[float]
    session_id: Optional[str]
    model: Optional[str]
    claude_code_version: Optional[str]
    duration_ms: Optional[int]
    duration_api_ms: Optional[int]
    num_turns: Optional[int]
    rate_limit: Optional[dict[str, Any]]
    wall_ms: int
    raw_result: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass
class _Stream:
    init: dict[str, Any] = field(default_factory=dict)
    rate_limit: Optional[dict[str, Any]] = None
    result: Optional[dict[str, Any]] = None
    last_stop_reason: Optional[str] = None
    bad_lines: int = 0


def build_argv(cfg: Config, binary: str, req: ClaudeRequest) -> list[str]:
    argv = [
        binary,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--model", req.model,
        "--tools", "",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--system-prompt", req.system_prompt,
    ]
    if req.effort:
        argv += ["--effort", req.effort]
    if req.schema is not None:
        argv += ["--json-schema", json.dumps(req.schema, separators=(",", ":"))]
    if cfg.uses_api_key:
        argv.append("--bare")
    argv += list(cfg.extra_args)
    return argv


async def run_claude(
    cfg: Config,
    binary: str,
    req: ClaudeRequest,
    *,
    on_rate_limit: Optional[Any] = None,
) -> ClaudeResult:
    """Run one call. Raises BridgeError; `on_rate_limit(info)` sees every quota event,
    including the ones that arrive on a failed call."""
    argv = build_argv(cfg, binary, req)
    env = cfg.child_env()
    started = time.monotonic()
    stream = _Stream()
    stderr_chunks: list[bytes] = []

    with tempfile.TemporaryDirectory(prefix="claude-bridge-") as workdir:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            env=env,
            start_new_session=True,
            limit=_STREAM_LIMIT,
        )
        try:
            await asyncio.wait_for(
                _drive(proc, req.prompt, stream, stderr_chunks, on_rate_limit),
                timeout=cfg.timeout_seconds,
            )
        except asyncio.TimeoutError:
            await _kill_group(proc)
            raise BridgeError(
                504, "timeout", f"claude did not finish within {cfg.timeout_seconds:g}s",
                details={"wall_ms": _ms_since(started)},
            ) from None
        except BaseException:
            # Cancelled request (client gone) or a bug: never leave a child behind.
            await _kill_group(proc)
            raise

    wall_ms = _ms_since(started)
    stderr = b"".join(stderr_chunks).decode("utf-8", "replace")[-_STDERR_TAIL:]
    return _interpret(req, stream, proc.returncode, stderr, wall_ms)


async def _drive(proc, prompt: str, stream: _Stream, stderr_chunks: list[bytes], on_rate_limit) -> None:
    async def feed() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the child died early; its exit code and stderr tell why
        finally:
            proc.stdin.close()

    async def read_stdout() -> None:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            _consume(line, stream, on_rate_limit)

    async def read_stderr() -> None:
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(65536)
            if not chunk:
                return
            stderr_chunks.append(chunk)
            # Keep only a bounded tail in memory.
            while sum(map(len, stderr_chunks)) > 4 * _STDERR_TAIL and len(stderr_chunks) > 1:
                stderr_chunks.pop(0)

    await asyncio.gather(feed(), read_stdout(), read_stderr())
    await proc.wait()


def _consume(line: bytes, stream: _Stream, on_rate_limit) -> None:
    try:
        event = json.loads(line)
    except ValueError:
        stream.bad_lines += 1
        return
    if not isinstance(event, dict):
        stream.bad_lines += 1
        return
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        stream.init = event
    elif kind == "rate_limit_event":
        info = event.get("rate_limit_info")
        if isinstance(info, dict):
            stream.rate_limit = info
            if on_rate_limit is not None:
                try:
                    on_rate_limit(info)
                except Exception:  # pragma: no cover - a guard bug must not fail the call
                    log.exception("quota callback failed")
    elif kind == "assistant":
        message = event.get("message")
        if isinstance(message, dict) and message.get("stop_reason"):
            stream.last_stop_reason = message["stop_reason"]
    elif kind == "result":
        stream.result = event


def _interpret(req: ClaudeRequest, stream: _Stream, returncode: Optional[int], stderr: str, wall_ms: int) -> ClaudeResult:
    result = stream.result
    details = {
        "returncode": returncode,
        "wall_ms": wall_ms,
        "claude_code_version": stream.init.get("claude_code_version"),
    }
    if result is None:
        if _looks_like_auth(stderr):
            raise BridgeError(424, "claude_auth", f"claude is not authenticated: {stderr.strip()[-500:]}", details=details)
        raise BridgeError(
            502, "claude_crashed",
            f"claude exited (rc={returncode}) without a result: {stderr.strip()[-500:] or 'no stderr'}",
            details=details,
        )

    details.update({
        "subtype": result.get("subtype"),
        "api_error_status": result.get("api_error_status"),
        "session_id": result.get("session_id"),
        "cost_usd_equivalent": result.get("total_cost_usd"),
    })
    text = result.get("result") if isinstance(result.get("result"), str) else ""

    if result.get("is_error") or str(result.get("subtype", "")).startswith("error"):
        raise _error_from_result(result, text, stream.rate_limit, stderr, details)

    structured = result.get("structured_output")
    if req.schema is not None:
        if structured is None:
            raise BridgeError(502, "schema_failed", "claude returned no structured_output for the schema", details=details)
        content = json.dumps(structured, ensure_ascii=False)
    else:
        content = text

    stop_reason = result.get("stop_reason") or stream.last_stop_reason
    finish_reason = "length" if stop_reason == "max_tokens" else "stop"

    return ClaudeResult(
        content=content,
        structured_output=structured,
        finish_reason=finish_reason,
        usage=result.get("usage") if isinstance(result.get("usage"), dict) else {},
        cost_usd=result.get("total_cost_usd"),
        session_id=result.get("session_id"),
        model=stream.init.get("model"),
        claude_code_version=stream.init.get("claude_code_version"),
        duration_ms=result.get("duration_ms"),
        duration_api_ms=result.get("duration_api_ms"),
        num_turns=result.get("num_turns"),
        rate_limit=stream.rate_limit,
        wall_ms=wall_ms,
        raw_result=result,
    )


def _error_from_result(result, text: str, rate_limit, stderr: str, details) -> BridgeError:
    status = result.get("api_error_status")
    message = (text or stderr or str(result.get("subtype")) or "claude error").strip()[:500]

    usage_limit = _USAGE_LIMIT_RE.search(text)
    rejected = isinstance(rate_limit, dict) and rate_limit.get("status") == "rejected"
    if status == 429 or usage_limit or rejected:
        return BridgeError(429, "rate_limited", message, retry_after=_retry_after(rate_limit, usage_limit), details=details)
    if status in _OVERLOAD_STATUSES:
        return BridgeError(503, "upstream_overloaded", message, details=details)
    if status in _AUTH_STATUSES or _looks_like_auth(text):
        return BridgeError(424, "claude_auth", message, details=details)
    if isinstance(status, int) and 400 <= status < 500:
        return BridgeError(400, "upstream_rejected", message, details=details)
    if "structured" in str(result.get("subtype", "")):
        return BridgeError(502, "schema_failed", message, details=details)
    return BridgeError(502, "claude_error", message, details=details)


def _retry_after(rate_limit, usage_limit) -> int:
    now = time.time()
    resets_at = None
    if isinstance(rate_limit, dict) and isinstance(rate_limit.get("resetsAt"), (int, float)):
        resets_at = rate_limit["resetsAt"]
    elif usage_limit:
        resets_at = int(usage_limit.group(1))
    if resets_at is None or resets_at <= now:
        return 60
    return int(resets_at - now) + 1


def _looks_like_auth(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _AUTH_MARKERS)


async def _kill_group(proc) -> None:
    if proc.returncode is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=_KILL_GRACE_SECONDS)
            return
        except asyncio.TimeoutError:
            continue


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
