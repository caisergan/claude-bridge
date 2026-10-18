import json
import os
import stat
import time
from dataclasses import replace
from pathlib import Path

import pytest

from claude_bridge.config import Config, ConfigError, load_config
from claude_bridge.quota import QuotaGuard
from claude_bridge.server import STATE, create_app

FAKE = Path(__file__).with_name("fake_claude.py")
TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SCHEMA = {
    "type": "object",
    "properties": {"sentiment": {"type": "string"}, "score": {"type": "number"}},
    "required": ["sentiment", "score"],
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def _fake_env(monkeypatch, tmp_path):
    FAKE.chmod(FAKE.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "invocation.json"))
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "ok")
    # Must never reach the child: the env allowlist keeps parent-session state out.
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")


def make_cfg(**overrides) -> Config:
    base = Config(
        binary=str(FAKE),
        passthrough_env=("FAKE_CLAUDE_SCENARIO", "FAKE_CLAUDE_LOG", "FAKE_FIVE_HOUR", "FAKE_SEVEN_DAY"),
        timeout_seconds=5,
    )
    return replace(base, **overrides)


@pytest.fixture
def client_for(aiohttp_client):
    async def factory(**overrides):
        cfg = make_cfg(**overrides)
        return await aiohttp_client(create_app(cfg, binary=str(FAKE), token=TOKEN))
    return factory


def chat_body(**extra):
    body = {
        "model": "sonnet",
        "messages": [
            {"role": "system", "content": "Classify the news."},
            {"role": "user", "content": "ACME beats earnings."},
        ],
        "temperature": 0.1,
        "max_tokens": 4096,
        "stream": False,
    }
    body.update(extra)
    return body


def schema_format():
    return {"type": "json_schema", "json_schema": {"name": "article_analysis", "strict": True, "schema": SCHEMA}}


def invocation(tmp_path):
    return json.loads((tmp_path / "invocation.json").read_text())


async def test_schema_call_returns_openai_envelope(client_for, tmp_path):
    client = await client_for()
    resp = await client.post("/v1/chat/completions", json=chat_body(
        response_format=schema_format(), reasoning_effort="low"), headers=AUTH)
    assert resp.status == 200
    data = await resp.json()

    choice = data["choices"][0]
    assert json.loads(choice["message"]["content"]) == {"sentiment": "positive", "score": 0.85}
    assert choice["finish_reason"] == "stop"
    assert data["usage"] == {
        "prompt_tokens": 1603, "completion_tokens": 80, "total_tokens": 1683,
        "prompt_tokens_details": {"cached_tokens": 400},
        "completion_tokens_details": {"reasoning_tokens": 12},
    }
    x = data["x_claude"]
    assert x["cost_usd_equivalent"] == 0.0123
    assert x["session_id"] == "sess-1"
    assert x["effort"] == "low"
    assert x["ignored_params"] == ["temperature", "max_tokens"]
    assert x["quota"]["windows"]["seven_day"]["utilization_percent"] == 50.0

    call = invocation(tmp_path)
    argv = call["argv"]
    assert argv[:3] == ["-p", "--output-format", "stream-json"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    for flag in ("--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
        assert flag in argv
    assert "--bare" not in argv
    assert argv[argv.index("--system-prompt") + 1] == "Classify the news."
    assert argv[argv.index("--effort") + 1] == "low"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == SCHEMA
    assert call["stdin"] == "ACME beats earnings."
    assert os.path.basename(call["cwd"]).startswith("claude-bridge-")
    assert not os.path.exists(call["cwd"]), "temp workdir must be removed"
    assert "CLAUDECODE" not in call["env"]
    assert "ANTHROPIC_API_KEY" not in call["env"]
    assert "DISABLE_AUTOUPDATER" in call["env"]


async def test_plain_text_call_without_schema(client_for, tmp_path):
    client = await client_for()
    resp = await client.post("/v1/chat/completions", json=chat_body(), headers=AUTH)
    assert resp.status == 200
    data = await resp.json()
    assert data["choices"][0]["message"]["content"] == "echo: ACME beats earnings."
    assert "--json-schema" not in invocation(tmp_path)["argv"]
    assert "--effort" not in invocation(tmp_path)["argv"]


async def test_auth_required(client_for):
    client = await client_for()
    assert (await client.post("/v1/chat/completions", json=chat_body())).status == 401
    bad = {"Authorization": "Bearer nope"}
    assert (await client.get("/v1/models", headers=bad)).status == 401
    models = await (await client.get("/v1/models", headers=AUTH)).json()
    assert "sonnet" in [m["id"] for m in models["data"]]
    # Health stays open (it carries no secrets) for local monitoring.
    assert (await client.get("/health")).status == 200


@pytest.mark.parametrize("body, needle", [
    (chat_body(stream=True), "stream"),
    (chat_body(model="gpt-5"), "not allowed"),
    (chat_body(messages=[{"role": "assistant", "content": "hi"}, {"role": "user", "content": "x"}]), "single-turn"),
    (chat_body(messages=[{"role": "system", "content": "only system"}]), "user message"),
    (chat_body(reasoning_effort="turbo"), "reasoning_effort"),
    (chat_body(response_format={"type": "json_schema", "json_schema": {}}), "schema"),
])
async def test_rejects_what_it_cannot_honour(client_for, body, needle):
    client = await client_for()
    resp = await client.post("/v1/chat/completions", json=body, headers=AUTH)
    assert resp.status == 400
    assert needle in (await resp.json())["error"]["message"]


async def test_effort_none_maps_to_low(client_for, tmp_path):
    client = await client_for()
    resp = await client.post("/v1/chat/completions", json=chat_body(reasoning_effort="none"), headers=AUTH)
    assert resp.status == 200
    argv = invocation(tmp_path)["argv"]
    assert argv[argv.index("--effort") + 1] == "low"


@pytest.mark.parametrize("scenario, status, code", [
    ("rate_limited", 429, "rate_limited"),
    ("overloaded", 503, "upstream_overloaded"),
    ("schema_fail", 502, "schema_failed"),
    ("no_structured", 502, "schema_failed"),
    ("crash", 502, "claude_crashed"),
    ("auth", 424, "claude_auth"),
])
async def test_error_mapping(client_for, monkeypatch, scenario, status, code):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", scenario)
    client = await client_for()
    resp = await client.post("/v1/chat/completions", json=chat_body(response_format=schema_format()), headers=AUTH)
    assert resp.status == status
    data = await resp.json()
    assert data["error"]["code"] == code
    if status == 429:
        assert 3500 <= int(resp.headers["Retry-After"]) <= 3602


async def test_length_stop_reason_maps_to_finish_length(client_for, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "length")
    client = await client_for()
    data = await (await client.post("/v1/chat/completions", json=chat_body(), headers=AUTH)).json()
    assert data["choices"][0]["finish_reason"] == "length"


async def test_timeout_kills_the_child(client_for, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "hang")
    client = await client_for(timeout_seconds=0.5)
    started = time.monotonic()
    resp = await client.post("/v1/chat/completions", json=chat_body(), headers=AUTH)
    assert resp.status == 504
    assert time.monotonic() - started < 5
    await __import__("asyncio").sleep(0.1)
    assert not _alive(invocation(tmp_path)["pid"])


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_cancelled_request_kills_the_child(monkeypatch, tmp_path):
    import asyncio
    from claude_bridge.runner import ClaudeRequest, run_claude

    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "hang")
    cfg = make_cfg(timeout_seconds=30)
    task = asyncio.create_task(run_claude(cfg, str(FAKE), ClaudeRequest("s", "p", "sonnet")))
    log = tmp_path / "invocation.json"
    for _ in range(100):
        if log.exists() and log.stat().st_size:
            break
        await asyncio.sleep(0.05)
    pid = invocation(tmp_path)["pid"]
    assert _alive(pid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.1)
    assert not _alive(pid)


async def test_quota_guard_blocks_after_a_call_crosses_the_limit(client_for, monkeypatch):
    monkeypatch.setenv("FAKE_SEVEN_DAY", "0.95")
    client = await client_for()
    first = await client.post("/v1/chat/completions", json=chat_body(), headers=AUTH)
    assert first.status == 200  # the call that reveals the level still completes

    second = await client.post("/v1/chat/completions", json=chat_body(), headers=AUTH)
    assert second.status == 429
    assert (await second.json())["error"]["code"] == "quota_guard"
    assert int(second.headers["Retry-After"]) > 80000

    health = await (await client.get("/health")).json()
    assert health["status"] == "quota_blocked"
    assert health["outcomes"] == {"ok": 1, "quota_blocked": 1}


async def test_queue_full_returns_503(client_for, monkeypatch):
    client = await client_for(max_concurrency=1, max_queue=0)
    state = client.server.app[STATE]
    await state.semaphore.acquire()  # simulate a call in flight
    try:
        resp = await client.post("/v1/chat/completions", json=chat_body(), headers=AUTH)
        assert resp.status == 503
        assert (await resp.json())["error"]["code"] == "queue_full"
    finally:
        state.semaphore.release()


def test_quota_guard_reopens_after_reset(tmp_path):
    now = 1_000_000
    guard = QuotaGuard({"five_hour": 80.0}, tmp_path / "q.json")
    guard.update({"status": "allowed", "unifiedWindows": {
        "five_hour": {"utilization": 0.85, "resetsAt": now + 100}}}, now=now)
    assert guard.check(now=now).blocked
    assert guard.check(now=now).retry_after == 101
    assert not guard.check(now=now + 101).blocked
    # Persisted: a restart keeps the block.
    assert QuotaGuard({"five_hour": 80.0}, tmp_path / "q.json").check(now=now).blocked


def test_quota_guard_honours_claude_rejection():
    now = 1_000_000
    guard = QuotaGuard({})
    guard.update({"status": "rejected", "resetsAt": now + 50, "rateLimitType": "seven_day"}, now=now)
    decision = guard.check(now=now)
    assert decision.blocked and "seven_day" in decision.reason


def test_load_config(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_BRIDGE_PORT", raising=False)
    monkeypatch.delenv("CLAUDE_BRIDGE_HOSTS", raising=False)
    path = tmp_path / "config.toml"
    path.write_text(
        '[server]\nhosts = ["127.0.0.1", "10.0.0.1"]\ntoken_file = "~/tok"\n'
        '[claude]\ndefault_model = "haiku"\ndefault_effort = "low"\nmax_concurrency = 2\n'
        '[quota.limits]\nseven_day = 70\n'
    )
    cfg = load_config(path)
    assert cfg.hosts == ("127.0.0.1", "10.0.0.1")
    assert cfg.token_file == Path(os.path.expanduser("~/tok"))
    assert cfg.default_model == "haiku" and cfg.default_effort == "low" and cfg.max_concurrency == 2
    assert cfg.quota_limits == {"five_hour": 80.0, "seven_day": 70.0}

    path.write_text('[claude]\ndefault_model = "gpt-5"\n')
    with pytest.raises(ConfigError):
        load_config(path)


def test_api_key_mode_adds_bare_and_key(tmp_path):
    key = tmp_path / "key"
    key.write_text("sk-test\n")
    cfg = make_cfg(api_key_file=key)
    assert cfg.child_env()["ANTHROPIC_API_KEY"] == "sk-test"
    from claude_bridge.runner import ClaudeRequest, build_argv
    assert "--bare" in build_argv(cfg, "claude", ClaudeRequest("s", "p", "sonnet"))
