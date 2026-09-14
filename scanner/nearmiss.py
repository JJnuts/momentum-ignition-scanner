"""Near-miss ("why not") log - 2026-09-15.

Alerts only tell us how the tokens we PICKED behaved. The tuning pass cannot
see whether a veto or the score bar is throwing away winners. This module
records, once per candidate episode, every decision that came close to an
alert and the single reason it did not become one, then labels it exactly
like a control (close labels, sampled candle path) so tune.py can compare
each reason's win rate with alerts and controls.

Kinds / reasons:
  score   eligible, no hard veto, score within `score_margin` below the
          chain's IGNITION bar                       reason = "score"
  veto    eligible, score >= bar, exactly ONE hard veto  reason = veto name
          (SAFETY:<x> collapses to SAFETY)
  policy  alertable but not sent                     reason = cooldown | hourly_cap
  late    no veto, score >= bar, but past the eligibility window
                                                      reason = "late"
Two vetoes at once are not a near miss (removing one would not have alerted).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable

from .clock import day_key

log = logging.getLogger("nearmiss")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "score_margin": 10.0,
    "cooldown_min": 20,      # one row per (chain, address, kind, reason) per cooldown
    "daily_max": 400,        # labels cost CU; cap rows per local day
}


def _settings(cfg: dict[str, Any] | None) -> dict[str, Any]:
    s = dict(DEFAULTS)
    for k, v in (cfg or {}).items():
        if not k.startswith("_"):
            s[k] = v
    return s


@dataclass
class NearMiss:
    kind: str
    reason: str
    score: float
    tier: str
    vetoes: list[str]
    margin: float | None     # score: bar - score; veto: score - bar; late: seconds past the window; policy: None


def classify(d: Any, ignition_bar: float, eligible_max_s: int, alert_outcome: str | None = None,
             score_margin: float = 10.0) -> NearMiss | None:
    """d = scoring.Decision. alert_outcome = Alerter.consider() result when d was alertable (sent|cooldown|hourly_cap|failed)."""
    hard = list(d.hard_vetoes)
    bar = float(ignition_bar)
    if d.alertable:
        if alert_outcome in ("cooldown", "hourly_cap"):
            return NearMiss("policy", alert_outcome, d.score, d.tier, hard, None)
        return None
    if d.eligible:
        if not hard and bar - float(score_margin) <= d.score < bar:
            return NearMiss("score", "score", d.score, d.tier, hard, round(bar - d.score, 2))
        if len(hard) == 1 and d.score >= bar:
            name = hard[0].split(":")[0] if hard[0].startswith("SAFETY:") else hard[0]
            return NearMiss("veto", name, d.score, d.tier, hard, round(d.score - bar, 2))
        return None
    if not hard and d.score >= bar and d.since_anchor_s is not None and d.since_anchor_s > int(eligible_max_s):
        return NearMiss("late", "late", d.score, d.tier, hard, float(d.since_anchor_s - int(eligible_max_s)))
    return None


class NearMissLog:
    def __init__(self, conn: sqlite3.Connection, settings: dict[str, Any] | None, labeler: Any = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.conn = conn
        self.s = _settings(settings)
        self.labeler = labeler
        self._clock = clock
        self.stats: dict[str, int] = {"recorded": 0, "dedup": 0, "daily_max": 0}

    @property
    def enabled(self) -> bool:
        return bool(self.s["enabled"])

    def _recent(self, chain: str, address: str, kind: str, reason: str, now: int) -> bool:
        r = self.conn.execute("SELECT 1 FROM near_misses WHERE chain=? AND address=? AND kind=? AND reason=? AND ts>? LIMIT 1",
                              (chain, address, kind, reason, now - int(self.s["cooldown_min"]) * 60)).fetchone()
        return r is not None

    def _today_count(self, now: int) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM near_misses WHERE ts>=?", (day_key(now),)).fetchone()[0])

    def record(self, chain: str, address: str, decision_id: int | None, nm: NearMiss, ts: int | None = None,
               price: float | None = None, liquidity: float | None = None) -> int | None:
        """Persist one near miss and enqueue its labels. Returns the row id, or None when deduped / capped."""
        if not self.enabled:
            return None
        now = int(self._clock()) if ts is None else int(ts)
        if self._recent(chain, address, nm.kind, nm.reason, now):
            self.stats["dedup"] += 1
            return None
        if self._today_count(now) >= int(self.s["daily_max"]):
            self.stats["daily_max"] += 1
            return None
        cur = self.conn.execute(
            "INSERT INTO near_misses(decision_id, chain, address, ts, kind, reason, score, tier, vetoes, margin, price, liquidity) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (decision_id, chain, address, now, nm.kind, nm.reason, nm.score, nm.tier, ",".join(nm.vetoes) or None,
             nm.margin, price, liquidity))
        rid = int(cur.lastrowid)
        if self.labeler is not None:
            self.labeler.enqueue("near_miss", rid, chain, address, now, price, liquidity)
        self.stats["recorded"] += 1
        return rid
