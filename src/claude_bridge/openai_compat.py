"""OpenAI chat-completions request/response translation.

Only the subset a single-shot analysis client needs is accepted: system (or
developer) messages become the system prompt, user messages become the prompt,
and `response_format.json_schema.schema` becomes `--json-schema`. Anything the
bridge cannot honour faithfully — streaming, multi-turn history, images, tool
calls — is rejected with 400 instead of being silently approximated.

Accepted but ignored: `temperature`, `max_tokens`, `service_tier`. Claude Code
has no per-call flag for them; `x_claude.ignored_params` lists which ones a
request carried.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from .config import CLAUDE_EFFORT_LEVELS, Config
from .runner import BridgeError, ClaudeRequest, ClaudeResult

_IGNORED_PARAMS = ("temperature", "top_p", "max_tokens", "max_completion_tokens", "service_tier", "seed")
# OpenAI-style levels Claude Code does not have, mapped to its lowest level.
_EFFORT_ALIASES = {"none": "low", "minimal": "low"}
_DEFAULT_SYSTEM_PROMPT = "Answer the user's request."


def invalid(message: str) -> BridgeError:
    return BridgeError(400, "invalid_request", message)


def parse_chat_request(body: Any, cfg: Config) -> tuple[ClaudeRequest, list[str]]:
    if not isinstance(body, dict):
        raise invalid("request body must be a JSON object")
    if body.get("stream"):
        raise invalid("stream=true is not supported")
    if body.get("tools") or body.get("functions"):
        raise invalid("tool calling is not supported")
    n = body.get("n")
    if n not in (None, 1):
        raise invalid("only n=1 is supported")

    model = body.get("model") or cfg.default_model
    if model not in cfg.models:
        raise invalid(f"model {model!r} is not allowed; allowed: {', '.join(cfg.models)}")

    system_parts: list[str] = []
    user_parts: list[str] = []
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise invalid("messages must be a non-empty list")
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise invalid(f"messages[{index}] must be an object")
        role = message.get("role")
        text = _message_text(message.get("content"), index)
        if role in ("system", "developer"):
            system_parts.append(text)
        elif role == "user":
            user_parts.append(text)
        else:
            raise invalid(f"messages[{index}]: role {role!r} is not supported (single-turn only)")
    if not user_parts:
        raise invalid("at least one user message is required")

    system_prompt = "\n\n".join(p for p in system_parts if p) or _DEFAULT_SYSTEM_PROMPT
    if len(system_prompt) > cfg.max_system_prompt_chars:
        raise invalid(f"system prompt is {len(system_prompt)} chars; limit {cfg.max_system_prompt_chars}")

    return (
        ClaudeRequest(
            system_prompt=system_prompt,
            prompt="\n\n".join(user_parts),
            model=model,
            effort=_effort(body, cfg),
            schema=_schema(body.get("response_format")),
        ),
        [key for key in _IGNORED_PARAMS if body.get(key) is not None],
    )


def _message_text(content: Any, index: int) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str):
                raise invalid(f"messages[{index}]: only text content parts are supported")
            texts.append(part["text"])
        return "\n".join(texts)
    raise invalid(f"messages[{index}].content must be a string or a list of text parts")


def _effort(body: dict[str, Any], cfg: Config) -> Optional[str]:
    value = body.get("reasoning_effort")
    if value is None and isinstance(body.get("reasoning"), dict):
        value = body["reasoning"].get("effort")
    if value in (None, ""):
        return cfg.default_effort
    effort = str(value).strip().lower()
    effort = _EFFORT_ALIASES.get(effort, effort)
    if effort in ("default", "inherit"):
        return cfg.default_effort
    if effort not in CLAUDE_EFFORT_LEVELS:
        raise invalid(f"reasoning_effort {value!r} is not supported; valid: {', '.join(CLAUDE_EFFORT_LEVELS)}")
    return effort


def _schema(response_format: Any) -> Optional[dict[str, Any]]:
    if response_format in (None, {}):
        return None
    if not isinstance(response_format, dict):
        raise invalid("response_format must be an object")
    kind = response_format.get("type")
    if kind in ("text", "json_object"):
        return None
    if kind != "json_schema":
        raise invalid(f"response_format.type {kind!r} is not supported")
    spec = response_format.get("json_schema")
    schema = spec.get("schema") if isinstance(spec, dict) else None
    if not isinstance(schema, dict):
        raise invalid("response_format.json_schema.schema must be an object")
    return schema


def chat_completion(
    req: ClaudeRequest,
    result: ClaudeResult,
    *,
    ignored_params: list[str],
    queue_wait_ms: int,
    quota: dict[str, Any],
) -> dict[str, Any]:
    usage = result.usage
    cache_read = _int(usage.get("cache_read_input_tokens"))
    cache_write = _int(usage.get("cache_creation_input_tokens"))
    prompt_tokens = _int(usage.get("input_tokens")) + cache_read + cache_write
    completion_tokens = _int(usage.get("output_tokens"))
    details = usage.get("output_tokens_details")
    thinking = _int(details.get("thinking_tokens")) if isinstance(details, dict) else 0

    return {
        "id": f"chatcmpl-{result.session_id or int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.content},
                "finish_reason": result.finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_tokens_details": {"cached_tokens": cache_read},
            "completion_tokens_details": {"reasoning_tokens": thinking},
        },
        "x_claude": {
            "resolved_model": result.model,
            "effort": req.effort,
            "structured": req.schema is not None,
            # API list price of the call. The subscription is not billed per
            # call; this is an equivalent for comparison, not a real charge.
            "cost_usd_equivalent": result.cost_usd,
            "session_id": result.session_id,
            "duration_ms": result.duration_ms,
            "duration_api_ms": result.duration_api_ms,
            "wall_ms": result.wall_ms,
            "queue_wait_ms": queue_wait_ms,
            "num_turns": result.num_turns,
            "cache_creation_input_tokens": cache_write,
            "claude_code_version": result.claude_code_version,
            "ignored_params": ignored_params,
            "quota": quota,
        },
    }


def error_body(error: BridgeError) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": {
            "message": error.message,
            "type": "claude_bridge_error",
            "code": error.code,
        }
    }
    if error.retry_after is not None:
        body["error"]["retry_after"] = error.retry_after
    if error.details:
        body["x_claude"] = error.details
    return body


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) else 0
