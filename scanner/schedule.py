"""Active window (T15b): a daily quiet block in LOCAL time during which Stage 0
does not scan the market (mode "off") or scans slowly (mode "slow").

User decision 2026-09-11: 09:00-15:00 Europe/Sofia is quiet (labeled win rate
28 % there vs 36 % in 00:00-08:00) and the CU budget cannot cover 24 h.

Only Stage 0 consults the schedule. Everything downstream of an alert keeps
running so nothing already started is lost: the labeler (+5/15/30/60 labels),
the rug watch (+10/30/60 re-checks), tape polls of still-active candidates
(they expire on their own 8 min after the last nomination) and the heartbeat.

config.schedule = {"enabled": true, "quiet_local": {"start": "09:00", "end": "15:00"},
                   "mode": "off" | "slow", "slow_interval_s": 300}
A window with start > end wraps past midnight (e.g. 22:00-06:00).
"""
from __future__ import annotations

from typing import Any

from . import clock

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "quiet_local": {"start": "09:00", "end": "15:00"},
    "mode": "off",
    "slow_interval_s": 300,
}


def _hhmm(s: str) -> int:
    try:
        h, m = str(s).strip().split(":")
        h, m = int(h), int(m)
    except ValueError as e:
        raise ValueError(f"schedule time {s!r} must be HH:MM") from e
    if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m != 0):
        raise ValueError(f"schedule time {s!r} out of range")
    return h * 60 + m


class Schedule:
    def __init__(self, settings: dict[str, Any] | None) -> None:
        s = dict(DEFAULTS)
        for k, v in (settings or {}).items():
            if not k.startswith("_"):
                s[k] = v
        q = dict(DEFAULTS["quiet_local"])
        q.update(s.get("quiet_local") or {})
        self.enabled = bool(s["enabled"])
        self.start_min = _hhmm(q["start"])
        self.end_min = _hhmm(q["end"])
        self.mode = str(s["mode"]).lower()
        if self.mode not in ("off", "slow"):
            raise ValueError(f"schedule.mode {self.mode!r} must be 'off' or 'slow'")
        self.slow_interval_s = int(s["slow_interval_s"])
        if self.start_min == self.end_min:
            self.enabled = False            # empty window

    # ---- state -----------------------------------------------------------------------------
    def in_quiet(self, ts: float) -> bool:
        if not self.enabled:
            return False
        m = clock.local_minutes(ts)
        if self.start_min < self.end_min:
            return self.start_min <= m < self.end_min
        return m >= self.start_min or m < self.end_min          # wraps midnight

    def state(self, ts: float) -> str:
        if not self.enabled:
            return "always-on"
        return f"quiet({self.mode})" if self.in_quiet(ts) else "active"

    def scan_interval(self, ts: float, base_interval_s: float) -> float | None:
        """Seconds until the next Stage-0 scan for a chain whose normal interval is base_interval_s.
        None = do not scan now (quiet, mode off)."""
        if not self.in_quiet(ts):
            return float(base_interval_s)
        if self.mode == "slow":
            return float(max(base_interval_s, self.slow_interval_s))
        return None

    def next_transition(self, ts: float) -> int:
        """Epoch seconds of the next quiet<->active boundary after ts."""
        day0 = clock.day_start(ts)
        m_now = clock.local_minutes(ts)
        candidates = []
        for m in (self.start_min, self.end_min):
            if m > m_now:
                candidates.append(day0 + m * 60)
        if candidates:
            return min(candidates)
        nxt = clock.next_day_start(ts)
        return nxt + min(self.start_min, self.end_min) * 60

    def quiet_hours(self) -> set[int]:
        """Local hours (0-23) that lie at least partly inside the quiet window (for the budget report)."""
        if not self.enabled:
            return set()
        hrs: set[int] = set()
        for h in range(24):
            lo, hi = h * 60, h * 60 + 60
            if self.start_min < self.end_min:
                if lo < self.end_min and hi > self.start_min:
                    hrs.add(h)
            elif lo < self.end_min or hi > self.start_min:
                hrs.add(h)
        return hrs

    def active_fraction(self) -> float:
        """Share of the day Stage 0 scans at full cadence (1.0 when disabled)."""
        if not self.enabled or self.mode == "slow":
            return 1.0
        span = (self.end_min - self.start_min) % (24 * 60)
        return 1.0 - span / (24 * 60)

    def describe(self) -> str:
        if not self.enabled:
            return "schedule disabled (scan 24h)"
        return (f"quiet {self.start_min // 60:02d}:{self.start_min % 60:02d}-{self.end_min // 60:02d}:{self.end_min % 60:02d} "
                f"{clock.tz_name()} mode={self.mode}" + (f" every {self.slow_interval_s}s" if self.mode == "slow" else ""))
