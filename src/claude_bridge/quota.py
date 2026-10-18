"""Quota guard fed by Claude Code's `rate_limit_event`.

Every `claude -p --output-format stream-json` run reports the subscription's
rate-limit windows, e.g.::

    {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "resetsAt": 1792358400, "rateLimitType": "five_hour",
        "unifiedWindows": {
            "five_hour": {"utilization": 0.33, "resetsAt": 1792358400},
            "seven_day": {"utilization": 0.58, "resetsAt": 1792569600}}}}

The bridge shares that quota with the operator's own interactive Claude Code
use, so it stops accepting calls once a configured window crosses its limit and
reopens by itself when the window resets. The snapshot only moves when a call
runs; a block therefore lasts until the window's own reset time, which is the
earliest moment the number can come down anyway.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("claude_bridge.quota")


def utilization_percent(value: Any) -> Optional[float]:
    """Claude reports a 0-1 fraction; tolerate a 0-100 percentage too."""
    if not isinstance(value, (int, float)):
        return None
    value = float(value)
    return round(value * 100.0 if value <= 1.0 else value, 1)


@dataclass(frozen=True)
class QuotaDecision:
    blocked: bool
    reason: Optional[str] = None
    retry_after: Optional[int] = None


class QuotaGuard:
    def __init__(self, limits: dict[str, float], state_file: Optional[Path] = None) -> None:
        self.limits = dict(limits)
        self.state_file = state_file
        self.info: Optional[dict[str, Any]] = None
        self.updated_at: Optional[float] = None
        self._load()

    def update(self, info: Optional[dict[str, Any]], *, now: Optional[float] = None) -> None:
        if not isinstance(info, dict):
            return
        self.info = info
        self.updated_at = now if now is not None else time.time()
        self._save()

    def windows(self) -> dict[str, dict[str, Any]]:
        windows = (self.info or {}).get("unifiedWindows")
        return windows if isinstance(windows, dict) else {}

    def effective_limits(self, requested: Optional[dict[str, float]] = None) -> dict[str, float]:
        """Per-request limits, never looser than the bridge's own configuration.

        A client (the analysis engine's panel setting) may tighten a window or
        add one; it can never raise a window above the configured ceiling.
        """
        limits = dict(self.limits)
        for name, value in (requested or {}).items():
            limits[name] = min(limits.get(name, 100.0), float(value))
        return limits

    def check(self, *, now: Optional[float] = None, limits: Optional[dict[str, float]] = None) -> QuotaDecision:
        now = now if now is not None else time.time()
        info = self.info or {}

        # Claude itself refused: honour it until its own reset.
        if info.get("status") == "rejected":
            resets_at = info.get("resetsAt")
            if isinstance(resets_at, (int, float)) and resets_at > now:
                window = info.get("rateLimitType") or "unknown"
                return QuotaDecision(True, f"claude rejected ({window} window)", _seconds_until(resets_at, now))

        for name, limit in (self.limits if limits is None else limits).items():
            window = self.windows().get(name)
            if not isinstance(window, dict):
                continue
            used = utilization_percent(window.get("utilization"))
            resets_at = window.get("resetsAt")
            if used is None or not isinstance(resets_at, (int, float)) or resets_at <= now:
                continue  # unknown, or the window has already reset since the snapshot
            if used >= limit:
                return QuotaDecision(
                    True,
                    f"{name} window at {used:.0f}% (limit {limit:.0f}%)",
                    _seconds_until(resets_at, now),
                )
        return QuotaDecision(False)

    def snapshot(self, *, now: Optional[float] = None, limits: Optional[dict[str, float]] = None) -> dict[str, Any]:
        effective = self.limits if limits is None else limits
        decision = self.check(now=now, limits=effective)
        return {
            "limits_percent": effective,
            "windows": {
                name: {
                    "utilization_percent": utilization_percent(w.get("utilization")),
                    "resets_at": w.get("resetsAt"),
                }
                for name, w in self.windows().items()
                if isinstance(w, dict)
            },
            "status": (self.info or {}).get("status"),
            "updated_at": self.updated_at,
            "blocked": decision.blocked,
            "reason": decision.reason,
            "retry_after": decision.retry_after,
        }

    def _load(self) -> None:
        if self.state_file is None or not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.info = data.get("info")
            self.updated_at = data.get("updated_at")
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable quota state %s: %s", self.state_file, exc)

    def _save(self) -> None:
        if self.state_file is None:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"info": self.info, "updated_at": self.updated_at}), encoding="utf-8")
            tmp.replace(self.state_file)
        except OSError as exc:
            log.warning("cannot persist quota state %s: %s", self.state_file, exc)


def _seconds_until(resets_at: float, now: float) -> int:
    return max(1, int(resets_at - now) + 1)
