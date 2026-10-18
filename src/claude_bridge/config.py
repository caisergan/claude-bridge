"""Bridge configuration: a TOML file, with a few environment overrides.

Every key has a default, so an empty file (or no file) is a valid config. Paths
accept ``~``. Secrets are never written in the config itself — only the path of
a file that holds them.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Claude Code's own effort levels (`claude --help`).
CLAUDE_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

DEFAULT_MODELS = (
    "sonnet",
    "opus",
    "haiku",
    "claude-sonnet-5-5",
    "claude-opus-5-5",
    "claude-haiku-5-5",
)

# The only variables a `claude` child inherits from the bridge's environment.
# Anything else — notably CLAUDECODE / CLAUDE_CODE_* from a parent session, or a
# stray ANTHROPIC_API_KEY that would silently switch billing — stays out.
BASE_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_RUNTIME_DIR",
)


# Set on every child. Each switch removes something Claude Code would otherwise
# load or do around an analysis call; measured on 2.1.302 via the init event:
# auto-memory path, telemetry plugin, Explore/Plan agents disappear.
HARDENING_ENV = {
    "DISABLE_AUTOUPDATER": "1",                       # never self-update mid-request
    "DISABLE_UPDATES": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",           # no per-cwd memory dir
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",            # no CLAUDE.md lookup
    "CLAUDE_CODE_DISABLE_ORG_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",
    "CLAUDE_CODE_DISABLE_POLICY_SKILLS": "1",
    "CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS": "1",
    "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
    "CLAUDE_CODE_DISABLE_ATTACHMENTS": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",  # telemetry / error reporting off
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
}

# Built-in plugins Claude Code 2.1.302 still reports with every switch above.
# They add nothing to the request (verified by capturing it); anything else in
# the init event fails the call — see runner.check_context.
DEFAULT_ALLOWED_PLUGINS = ("cc-plugin-agents-md", "cc-plugin-plugin-authoring")


class ConfigError(ValueError):
    """The configuration cannot be used to start the bridge."""


@dataclass(frozen=True)
class Config:
    # [server]
    hosts: tuple[str, ...] = ("127.0.0.1",)
    port: int = 8320
    token_file: Optional[Path] = None

    # [claude]
    binary: str = "claude"
    default_model: str = "sonnet"
    models: tuple[str, ...] = DEFAULT_MODELS
    default_effort: Optional[str] = None
    timeout_seconds: float = 120.0
    max_concurrency: int = 3
    max_queue: int = 20
    max_system_prompt_chars: int = 100_000
    config_dir: Optional[Path] = None
    oauth_token_file: Optional[Path] = None
    api_key_file: Optional[Path] = None
    passthrough_env: tuple[str, ...] = ()
    allowed_plugins: tuple[str, ...] = DEFAULT_ALLOWED_PLUGINS
    # Fixed, empty working directory shared by every call. A fixed path keeps
    # Claude Code's environment block byte-identical between calls (a random
    # temp dir would change it every time); the runner refuses to start while
    # it contains anything, so no AGENTS.md/CLAUDE.md can be planted there.
    workdir: Path = field(default_factory=lambda: Path(os.path.expanduser("~/.local/share/claude-bridge/cwd")))
    extra_args: tuple[str, ...] = ()

    # [quota] — window name -> max utilization in percent (0-100).
    quota_limits: dict[str, float] = field(
        default_factory=lambda: {"five_hour": 80.0, "seven_day": 90.0}
    )
    state_file: Optional[Path] = None

    def resolved_binary(self) -> str:
        found = shutil.which(self.binary)
        if not found:
            raise ConfigError(f"claude binary not found: {self.binary!r}")
        return found

    def read_token(self) -> str:
        if self.token_file is None:
            raise ConfigError("server.token_file is required (the bridge never runs unauthenticated)")
        return _read_secret(self.token_file, "server.token_file")

    def child_env(self) -> dict[str, str]:
        """Environment for one `claude` child process."""
        env = {
            key: os.environ[key]
            for key in (*BASE_ENV_ALLOWLIST, *self.passthrough_env)
            if key in os.environ
        }
        env.update(HARDENING_ENV)
        if self.config_dir is not None:
            env["CLAUDE_CONFIG_DIR"] = str(self.config_dir)
        if self.oauth_token_file is not None:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = _read_secret(self.oauth_token_file, "claude.oauth_token_file")
        if self.api_key_file is not None:
            env["ANTHROPIC_API_KEY"] = _read_secret(self.api_key_file, "claude.api_key_file")
        return env

    @property
    def uses_api_key(self) -> bool:
        # `--bare` ignores OAuth entirely, so it is only safe with an API key.
        return self.api_key_file is not None


def _read_secret(path: Path, label: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"{label}: cannot read {path}: {exc}") from exc
    if not value:
        raise ConfigError(f"{label}: {path} is empty")
    return value


def _path(value: Any) -> Optional[Path]:
    if value in (None, ""):
        return None
    return Path(os.path.expanduser(str(value)))


def _str_tuple(value: Any, label: str) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{label} must be a list of strings")
    return tuple(value)


def normalize_effort(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    effort = str(value).strip().lower()
    if effort in ("default", "inherit"):
        return None
    if effort not in CLAUDE_EFFORT_LEVELS:
        raise ConfigError(f"unknown effort {value!r}; valid: {', '.join(CLAUDE_EFFORT_LEVELS)}")
    return effort


def load_config(path: Optional[Path]) -> Config:
    raw: dict[str, Any] = {}
    if path is not None:
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError(f"config file not found: {path}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    server = raw.get("server", {})
    claude = raw.get("claude", {})
    quota = raw.get("quota", {})
    defaults = Config()

    limits = dict(defaults.quota_limits)
    for key, value in (quota.get("limits") or {}).items():
        if not isinstance(value, (int, float)) or not 0 < value <= 100:
            raise ConfigError(f"quota.limits.{key} must be a percentage in (0, 100]")
        limits[key] = float(value)

    env_port = os.environ.get("CLAUDE_BRIDGE_PORT")
    env_hosts = os.environ.get("CLAUDE_BRIDGE_HOSTS")

    cfg = Config(
        hosts=_str_tuple(env_hosts.split(","), "CLAUDE_BRIDGE_HOSTS") if env_hosts
        else _str_tuple(server.get("hosts", list(defaults.hosts)), "server.hosts"),
        port=int(env_port or server.get("port", defaults.port)),
        token_file=_path(os.environ.get("CLAUDE_BRIDGE_TOKEN_FILE") or server.get("token_file")),
        binary=str(claude.get("binary", defaults.binary)),
        default_model=str(claude.get("default_model", defaults.default_model)),
        models=_str_tuple(claude.get("models", list(defaults.models)), "claude.models"),
        default_effort=normalize_effort(claude.get("default_effort")),
        timeout_seconds=float(claude.get("timeout_seconds", defaults.timeout_seconds)),
        max_concurrency=int(claude.get("max_concurrency", defaults.max_concurrency)),
        max_queue=int(claude.get("max_queue", defaults.max_queue)),
        max_system_prompt_chars=int(claude.get("max_system_prompt_chars", defaults.max_system_prompt_chars)),
        config_dir=_path(claude.get("config_dir")),
        oauth_token_file=_path(claude.get("oauth_token_file")),
        api_key_file=_path(claude.get("api_key_file")),
        passthrough_env=_str_tuple(claude.get("passthrough_env", []), "claude.passthrough_env"),
        allowed_plugins=_str_tuple(
            claude.get("allowed_plugins", list(defaults.allowed_plugins)), "claude.allowed_plugins"
        ),
        extra_args=_str_tuple(claude.get("extra_args", []), "claude.extra_args"),
        workdir=_path(claude.get("workdir")) or defaults.workdir,
        quota_limits=limits,
        state_file=_path(quota.get("state_file")),
    )
    if cfg.default_model not in cfg.models:
        raise ConfigError(f"claude.default_model {cfg.default_model!r} is not in claude.models")
    if cfg.max_concurrency < 1 or cfg.max_queue < 0 or cfg.timeout_seconds <= 0:
        raise ConfigError("max_concurrency >= 1, max_queue >= 0 and timeout_seconds > 0 are required")
    if cfg.config_dir is not None:
        personal = {Path(os.path.expanduser("~/.claude")).resolve(), Path(os.path.expanduser("~")).resolve()}
        if cfg.config_dir.resolve() in personal:
            raise ConfigError(
                f"claude.config_dir {cfg.config_dir} is your own Claude Code setup; "
                "give the bridge its own directory (e.g. ~/.local/share/claude-bridge/claude-home)"
            )
    if cfg.oauth_token_file is not None and cfg.api_key_file is not None:
        raise ConfigError("set claude.oauth_token_file OR claude.api_key_file, not both")
    return cfg
