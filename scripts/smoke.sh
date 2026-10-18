#!/usr/bin/env bash
# Smoke test against a running bridge: health, models, one plain call, one
# schema call. Spends two small real Claude calls (default model: haiku).
#   scripts/smoke.sh [base_url] [model]
set -euo pipefail
BASE="${1:-http://127.0.0.1:8320}"
MODEL="${2:-haiku}"
TOKEN="$(cat "${CLAUDE_BRIDGE_TOKEN_FILE:-$HOME/.config/claude-bridge/token}")"
H=(-H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json")

echo "== /health";  curl -fsS "$BASE/health"; echo
echo "== /v1/models"; curl -fsS "${H[@]}" "$BASE/v1/models"; echo

echo "== plain text"
curl -sS "${H[@]}" "$BASE/v1/chat/completions" -d @- <<JSON
{"model": "$MODEL", "reasoning_effort": "low",
 "messages": [{"role": "system", "content": "You are a connectivity check. Reply with exactly: OK"},
              {"role": "user", "content": "ping"}]}
JSON
echo

echo "== json schema"
curl -sS "${H[@]}" "$BASE/v1/chat/completions" -d @- <<JSON
{"model": "$MODEL", "reasoning_effort": "low",
 "messages": [{"role": "system", "content": "Classify the sentiment of the headline."},
              {"role": "user", "content": "ACME beats earnings estimates and raises full-year guidance."}],
 "response_format": {"type": "json_schema", "json_schema": {"name": "check", "strict": true, "schema":
   {"type": "object", "additionalProperties": false, "required": ["sentiment", "confidence"],
    "properties": {"sentiment": {"type": "string", "enum": ["positive", "negative", "neutral"]},
                   "confidence": {"type": "number"}}}}}}
JSON
echo
