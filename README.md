# claude-bridge

A small OpenAI-compatible HTTP service in front of headless Claude Code
(`claude -p`). Each request runs one throwaway, locked-down Claude Code process
and returns its answer as a `chat.completion`. A JSON schema in
`response_format` is enforced by Claude Code itself (`--json-schema`), so the
reply is a schema-valid object, not "JSON-looking text".

It was built to let a Dockerised service (trade-station's sentiment engine)
use a Claude subscription through the same client it already uses for
CLIProxyAPI, but nothing in it is specific to that project.

```
client (e.g. a container)
   │  POST http://host.docker.internal:8320/v1/chat/completions
   ▼
claude-bridge  (host, user systemd unit)
   │  claude -p --output-format stream-json --verbose
   │           --system-prompt <system> --json-schema <schema>
   │           --model <m> --effort <e> --tools "" --setting-sources ""
   │           --strict-mcp-config --no-session-persistence --disable-slash-commands
   │  (user message on stdin, empty temp working directory)
   ▼
Claude (subscription login, long-lived OAuth token, or API key)
```

## Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /v1/chat/completions` | Bearer | One single-turn call. |
| `GET /v1/models` | Bearer | Allowed model aliases (doubles as a credential check). |
| `GET /health` | none | Version, active/queued calls, outcome counters, last error, quota snapshot. No secrets. |

### Request

```json
{
  "model": "sonnet",
  "messages": [
    {"role": "system", "content": "…instructions…"},
    {"role": "user", "content": "…article…"}
  ],
  "reasoning_effort": "low",
  "response_format": {"type": "json_schema", "json_schema": {"name": "x", "strict": true, "schema": {…}}}
}
```

- `system` / `developer` messages are joined into the system prompt (it
  *replaces* Claude Code's default prompt); `user` messages are joined into the
  prompt.
- `reasoning_effort`: `low | medium | high | xhigh | max`; `none`/`minimal` map
  to `low`; absent → `claude.default_effort` (empty = Claude Code's default).
- `response_format`: `json_schema` → enforced schema; `text` / `json_object` /
  absent → plain text answer.
- **Rejected with 400** rather than approximated: `stream: true`, assistant or
  tool messages (multi-turn), non-text content parts, tools, `n > 1`, a model
  not in `claude.models`.
- **Accepted but ignored** (Claude Code has no per-call flag):
  `temperature`, `top_p`, `max_tokens`, `max_completion_tokens`,
  `service_tier`, `seed`. The response lists them in `x_claude.ignored_params`.

### Response

A standard `chat.completion` (`choices[0].message.content` is the JSON text of
`structured_output` for schema calls), plus `usage` (`prompt_tokens` includes
cache reads and writes) and an `x_claude` object:

```json
"x_claude": {
  "resolved_model": "claude-sonnet-5-5", "effort": "low", "structured": true,
  "cost_usd_equivalent": 0.0123,
  "session_id": "…", "duration_ms": 900, "duration_api_ms": 700, "wall_ms": 2100,
  "queue_wait_ms": 0, "num_turns": 2, "cache_creation_input_tokens": 1200,
  "claude_code_version": "2.1.302",
  "context": {"tools": ["StructuredOutput"], "mcp_servers": [], "skills": [],
              "plugins": ["cc-plugin-agents-md", "cc-plugin-plugin-authoring"], "memory_paths": null},
  "ignored_params": ["temperature", "max_tokens"],
  "quota": {"windows": {"five_hour": {"utilization_percent": 35.0, "resets_at": 1792358400}, …},
            "limits_percent": {"five_hour": 80, "seven_day": 90}, "blocked": false, …}
}
```

`cost_usd_equivalent` is what Claude Code reports at API list prices. On a
subscription nothing is billed per call — treat it as a comparison figure, not
spend.

### Errors

OpenAI-style `{"error": {"message", "type", "code"}}`, with `x_claude` details
where available.

| HTTP | `code` | Meaning | Retry? |
|---|---|---|---|
| 400 | `invalid_request`, `upstream_rejected` | The request cannot be served as sent. | no |
| 401 | `unauthorized` | Missing/wrong bearer token. | no |
| 424 | `claude_auth` | Claude Code is not logged in or its credential was refused. An operator must act. | no |
| 424 | `context_guard` | Claude Code loaded more than the minimum context, or the workdir is not empty (see Isolation). An operator must act. | no |
| 429 | `rate_limited` | Claude refused on quota; `Retry-After` = seconds to the window reset. | after reset |
| 429 | `quota_guard` | The bridge's own limit (below) is reached. `Retry-After` as above. | after reset |
| 502 | `schema_failed` | No schema-valid `structured_output`. | yes |
| 502 | `claude_crashed`, `claude_error` | Claude Code exited without a usable result. | yes |
| 503 | `upstream_overloaded` | Anthropic 5xx/529. | yes |
| 503 | `queue_full` | More than `max_queue` requests waiting. `Retry-After: 5`. | yes |
| 504 | `timeout` | Exceeded `timeout_seconds`; the process group was killed. | yes |

A client that hangs up (e.g. its own deadline expired) cancels the request and
the Claude Code process is killed — no unseen work keeps spending quota.

## Quota guard

Every `claude -p` run reports the subscription's rate-limit windows
(`rate_limit_event`: 5-hour and 7-day utilization with reset times). The bridge
keeps the latest snapshot and refuses new calls (429 `quota_guard`) once a
window in `[quota.limits]` reaches its percentage, or while Claude itself
reports `rejected`. It reopens on its own when the window's reset time passes.

The quota is **shared with your own interactive Claude Code use**, which the
bridge only sees on its next call. Set limits so your own work keeps headroom.
With `quota.state_file` set, a restart keeps an active block.

## Isolation — the same minimal context on every call

Measured on Claude Code 2.1.302 by capturing the API request it sends: with
the bridge's flags and environment the request contains only

- Claude Code's billing-header line and one "You are a Claude agent…" line,
- **your system prompt** (cached for 1 h),
- an account e-mail reminder (injected by Claude Code for claude.ai logins),
- **your user message**,
- an environment block (working directory, OS, model name, date),
- the `StructuredOutput` tool (only when a schema is given).

That is ~1k tokens of Claude Code framing; no skills, MCP servers, CLAUDE.md,
memory, hooks, plugins' content or Claude Code tools. Three layers keep it so:

1. **Flags:** `--tools ""`, `--setting-sources ""` (no user/project/local
   settings, hence no hooks or user plugins), `--strict-mcp-config`,
   `--disable-slash-commands`, `--no-session-persistence`.
2. **Environment:** an allowlist (`PATH`, `HOME`, `USER`, locale, `XDG_*`,
   `TMPDIR`, plus `claude.passthrough_env`) — parent Claude Code session
   variables (`CLAUDECODE`, `CLAUDE_CODE_*`) and a stray `ANTHROPIC_API_KEY`
   never reach the child — plus `HARDENING_ENV` (`config.py`): no auto-memory,
   no CLAUDE.md lookup, no bundled/policy skills, no Explore/Plan agents, no
   telemetry or other non-essential traffic, no self-update.
3. **Context guard, every call:** Claude Code's `init` event, emitted before
   the first API request, lists what it loaded. Anything beyond the expected
   minimum — a tool other than `StructuredOutput`, an MCP server, a skill, a
   slash command, a memory path, a plugin not in `claude.allowed_plugins` —
   kills the process before any quota is spent and fails the call with
   **424 `context_guard`**. A Claude Code update that starts loading something
   new stops the bridge instead of silently changing what every call sees.
   The loaded set is returned in `x_claude.context` and `/health.last_context`.

The working directory is a fixed, empty, `0700` directory
(`claude.workdir`, default `~/.local/share/claude-bridge/cwd`). Fixed, so the
environment block is byte-identical between calls (a random temp dir would
change the request every time and defeat caching of retries); checked before
every call, so nothing (e.g. an `AGENTS.md`) can be planted there — a
non-empty workdir also fails with 424 `context_guard`.

The bridge binds only the configured addresses (default loopback). Every
`/v1/*` call needs the bearer token from `server.token_file`; the bridge
refuses to start without one.

## Authentication modes

| Config | What runs |
|---|---|
| nothing set | the host's current `claude` login (`~/.claude`) |
| `oauth_token_file` + `config_dir` (recommended) | a long-lived token from `claude setup-token` as `CLAUDE_CODE_OAUTH_TOKEN`, optionally with its own `CLAUDE_CONFIG_DIR` so it never touches your interactive login |
| `api_key_file` | `ANTHROPIC_API_KEY` + `--bare` (pay-per-token API; `--bare` ignores OAuth, so it is only used here) |

Check the Claude Code / Anthropic terms for your plan before pointing a
subscription login at automated workloads.

## Install

Requires Python ≥ 3.11 and a working `claude` CLI on the host.

Runtime files live apart from the source checkout and from your own Claude
Code setup, so developing the bridge or using Claude Code interactively never
collides with running analyses:

| What | Where |
|---|---|
| source (development) | `~/projects/claude-bridge` |
| installed service (pinned commit) | `~/.local/share/claude-bridge/venv` |
| Claude Code config dir of the bridge (`CLAUDE_CONFIG_DIR`) | `~/.local/share/claude-bridge/claude-home` |
| empty working directory | `~/.local/share/claude-bridge/cwd` |
| quota state | `~/.local/share/claude-bridge/state/` |
| config + secrets (0700) | `~/.config/claude-bridge/` |

```bash
# Install a pinned commit (source edits do not reach the service until you reinstall).
R=~/.local/share/claude-bridge
mkdir -p $R/claude-home $R/cwd $R/state && chmod 700 $R $R/claude-home $R/cwd
uv venv -p 3.11 $R/venv
uv pip install -p $R/venv "claude-bridge @ git+file://$HOME/projects/claude-bridge@$(git -C ~/projects/claude-bridge rev-parse HEAD)"

mkdir -p ~/.config/claude-bridge && chmod 700 ~/.config/claude-bridge
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.config/claude-bridge/token
chmod 600 ~/.config/claude-bridge/token
cp ~/projects/claude-bridge/deploy/config.example.toml ~/.config/claude-bridge/config.toml   # then edit

# Separate long-lived Claude token (interactive, opens a browser login once):
claude setup-token
read -rs T && printf '%s' "$T" > ~/.config/claude-bridge/oauth-token && unset T
chmod 600 ~/.config/claude-bridge/oauth-token

cp deploy/claude-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-bridge
journalctl --user -u claude-bridge -f
```

Containers reach the host as `host.docker.internal` (add
`extra_hosts: ["host.docker.internal:host-gateway"]` in compose if needed); bind
the bridge to that gateway address (`10.0.0.1` on a docker0 configured with
`10.0.0.0/24`) next to `127.0.0.1`.

Smoke test (two small real calls, Haiku by default):

```bash
scripts/smoke.sh http://127.0.0.1:8320 haiku
```

Re-run it after every Claude Code update — flags and stream-json shapes can
change. If an update makes calls fail with `context_guard`, read the reported
problem, check the new item is harmless (capture a request if unsure), and only
then add it to `claude.allowed_plugins` or the hardening env.

## Development

```bash
uv pip install -p .venv -e '.[dev]'
.venv/bin/pytest -q
```

Tests run against `tests/fake_claude.py`, which replays the stream-json event
shapes of a real Claude Code 2.1.302 call (success, plain text, 429, 529,
schema failure, crash, auth failure, hang, and init events that leak tools,
MCP servers, memory or plugins). No real Claude call is made.

## Configuration reference

See `deploy/config.example.toml`. Environment overrides:
`CLAUDE_BRIDGE_CONFIG`, `CLAUDE_BRIDGE_HOSTS` (comma-separated),
`CLAUDE_BRIDGE_PORT`, `CLAUDE_BRIDGE_TOKEN_FILE`, `CLAUDE_BRIDGE_LOG_LEVEL`.
