#!/usr/bin/env python3
"""Stand-in for `claude` in tests.

Emits the same stream-json event shapes Claude Code 2.1.302 produced in a real
call (2026-10-18). FAKE_CLAUDE_SCENARIO picks the behaviour; FAKE_CLAUDE_LOG,
when set, receives argv/stdin/cwd/env so tests can assert on the invocation.
"""

import json
import os
import sys
import time

if "--version" in sys.argv:
    print("9.9.9 (Claude Code)")
    sys.exit(0)

scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "ok")
prompt = sys.stdin.read()
argv = sys.argv[1:]

if os.environ.get("FAKE_CLAUDE_LOG"):
    with open(os.environ["FAKE_CLAUDE_LOG"], "w") as fh:
        json.dump({"pid": os.getpid(), "argv": argv, "stdin": prompt, "cwd": os.getcwd(), "env": sorted(os.environ)}, fh)


def arg(name):
    return argv[argv.index(name) + 1] if name in argv else None


def emit(event):
    print(json.dumps(event), flush=True)


now = int(time.time())
five_hour = float(os.environ.get("FAKE_FIVE_HOUR", "0.10"))
seven_day = float(os.environ.get("FAKE_SEVEN_DAY", "0.50"))
rate_limit = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "rejected" if scenario == "rate_limited" else "allowed",
        "resetsAt": now + 3600,
        "rateLimitType": "five_hour",
        "unifiedWindows": {
            "five_hour": {"utilization": five_hour, "resetsAt": now + 3600},
            "seven_day": {"utilization": seven_day, "resetsAt": now + 86400},
        },
    },
}
usage = {
    "input_tokens": 3,
    "cache_creation_input_tokens": 1200,
    "cache_read_input_tokens": 400,
    "output_tokens": 80,
    "output_tokens_details": {"thinking_tokens": 12},
}

emit({"type": "system", "subtype": "init", "model": "claude-sonnet-5-5", "claude_code_version": "9.9.9",
      "tools": ["StructuredOutput"], "session_id": "sess-1"})

if scenario == "crash":
    sys.stderr.write("boom: something broke\n")
    sys.exit(3)
if scenario == "auth":
    sys.stderr.write("Invalid API key · Please run /login\n")
    sys.exit(1)
if scenario == "hang":
    time.sleep(60)
    sys.exit(0)

schema = arg("--json-schema")
structured = {"sentiment": "positive", "score": 0.85} if schema else None
text = json.dumps(structured) if schema else f"echo: {prompt}"
result = {
    "type": "result", "subtype": "success", "is_error": False, "api_error_status": None,
    "result": text, "structured_output": structured, "stop_reason": "tool_use" if schema else "end_turn",
    "session_id": "sess-1", "total_cost_usd": 0.0123, "duration_ms": 900, "duration_api_ms": 700,
    "num_turns": 2 if schema else 1, "usage": usage,
}

if scenario == "rate_limited":
    emit(rate_limit)
    result.update(is_error=True, subtype="error_during_execution", api_error_status=429,
                  result="API Error: 429 rate limited", structured_output=None)
elif scenario == "overloaded":
    emit(rate_limit)
    result.update(is_error=True, subtype="error_during_execution", api_error_status=529,
                  result="API Error: 529 Overloaded", structured_output=None)
elif scenario == "schema_fail":
    emit(rate_limit)
    result.update(is_error=True, subtype="error_max_structured_output_retries",
                  result="Failed to provide valid structured output", structured_output=None)
elif scenario == "no_structured":
    emit(rate_limit)
    result.update(structured_output=None)
elif scenario == "length":
    emit(rate_limit)
    result.update(stop_reason="max_tokens")
else:
    emit(rate_limit)

emit(result)
