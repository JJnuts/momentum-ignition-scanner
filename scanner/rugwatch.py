"""Rug watch (T12) - re-checks every ALERTED token at +10/+30/+60 minutes.

A warning fires when, relative to the alert:
  LIQUIDITY_DROP  liquidity fell by >= liq_drop_pct (default 40%)
  SAFETY_FLIP     the safety verdict flipped to UNSAFE, or new hard reasons
                  appeared (honeypot armed, tax hiked, authority set ...)
One warning per alert per reason (never repeated across later re-checks).
Liquidity source order: a scan_rows snapshot within +-match_window (free) ->
market_data_single (8 CU, Lite+). Safety is re-checked with force=True.
T13 delivers rows with warned=1 and delivered_ts NULL.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable

from .birdeye import BirdeyeClient, BirdeyeError, EndpointUnavailable
from .config import ChainConfig
from .ledger import CULedger
from .clock import day_key
from .plans import cu_cost, endpoint_available

log = logging.getLogger("rugwatch")


@dataclass
class RugTickStats:
    due: int = 0
    done: int = 0
    failed: int = 0
    pending: int = 0
    warnings: int = 0
    cu: int = 0


class RugWatch:
    def __init__(self, conn: sqlite3.Connection, client: BirdeyeClient | None, ledger: CULedger | None,
                 safety: Any, settings: dict[str, Any], plan: str, daily_cu_cap: int,
                 clock: Callable[[], float] = time.time) -> None:
        self.conn = conn
        self.client = client
        self.ledger = ledger
        self.safety = safety                 # SafetyChecker (check(ch, address, force=True))
        self.plan = plan
        self.daily_cu_cap = daily_cu_cap
        self._clock = clock
        self.minutes: list[int] = [int(m) for m in settings.get("recheck_minutes", [10, 30, 60])]
        self.liq_drop_pct = float(settings.get("liq_drop_pct", 40.0))
        self.match_window_s = int(settings.get("scan_row_match_window_s", 120))
        self.grace_s = int(settings.get("grace_s", 900))
        self.daily_cu_budget = int(settings.get("daily_cu_budget", 20_000))
        self.cu_today = 0
        self._day = day_key(clock())

    # ---- scheduling -------------------------------------------------------------------------
    def schedule(self, alert_id: int, chain: str, address: str, alert_ts: int, liquidity: float | None,
                 safety_verdict: str | None) -> list[int]:
        ids: list[int] = []
        for m in self.minutes:
            cur = self.conn.execute(
                "INSERT INTO rug_checks(alert_id, chain, address, alert_ts, minute, due_ts, liq_then, safety_then) "
                "VALUES(?,?,?,?,?,?,?,?)", (alert_id, chain, address, alert_ts, m, alert_ts + m * 60, liquidity, safety_verdict))
            ids.append(int(cur.lastrowid))
        return ids

    # ---- sources --------------------------------------------------------------------------------
    def _budget_ok(self, cu: int) -> bool:
        d = day_key(self._clock())
        if d != self._day:
            self._day, self.cu_today = d, 0
        if self.cu_today + cu > self.daily_cu_budget:
            return False
        if self.ledger is not None and self.ledger.total_since(d) + cu > self.daily_cu_cap:
            return False
        return True

    def _scan_row_liquidity(self, chain: str, address: str, at_ts: int) -> float | None:
        r = self.conn.execute(
            "SELECT liquidity FROM scan_rows WHERE chain=? AND address=? AND ts BETWEEN ? AND ? AND liquidity IS NOT NULL "
            "ORDER BY ABS(ts - ?) LIMIT 1", (chain, address, at_ts - self.match_window_s, at_ts + self.match_window_s, at_ts)).fetchone()
        return float(r["liquidity"]) if r else None

    async def _market_liquidity(self, ch: ChainConfig, address: str) -> tuple[float | None, int]:
        if self.client is None or not endpoint_available("market_data_single", self.plan):
            return None, 0
        cu, _ = cu_cost("market_data_single")
        if not self._budget_ok(cu):
            return None, 0
        try:
            d = await self.client.market_data_single(ch.birdeye_chain, address)
        except (EndpointUnavailable, BirdeyeError) as e:
            log.warning("market data failed for %s: %s", address[:8], e)
            return None, 0
        self.cu_today += cu
        liq = d.get("liquidity")
        return (float(liq) if liq is not None else None), cu

    def _already_warned(self, alert_id: int, reason: str) -> bool:
        r = self.conn.execute("SELECT 1 FROM rug_checks WHERE alert_id=? AND warned=1 AND reason LIKE ? LIMIT 1",
                              (alert_id, f"%{reason}%")).fetchone()
        return r is not None

    # ---- tick -----------------------------------------------------------------------------------
    async def tick(self, chains: dict[str, ChainConfig], now: int | None = None) -> RugTickStats:
        now = int(self._clock()) if now is None else int(now)
        st = RugTickStats()
        due = self.conn.execute("SELECT * FROM rug_checks WHERE status='pending' AND due_ts<=? ORDER BY due_ts", (now,)).fetchall()
        st.due = len(due)
        for r in due:
            ch = chains.get(r["chain"])
            if ch is None:
                continue
            liq_now = self._scan_row_liquidity(r["chain"], r["address"], int(r["due_ts"]))
            if liq_now is None:
                liq_now, cu = await self._market_liquidity(ch, r["address"])
                st.cu += cu
            safety_now = None
            safety_reasons: list[str] = []
            if self.safety is not None:
                try:
                    sr = await self.safety.check(ch, r["address"], force=True)
                    safety_now, safety_reasons = sr.verdict, list(sr.reasons)
                except Exception as e:  # noqa: BLE001
                    log.warning("safety re-check failed for %s: %s", r["address"][:8], e)
            if liq_now is None and safety_now is None:
                if now - int(r["due_ts"]) > self.grace_s:
                    self.conn.execute("UPDATE rug_checks SET status='failed', done_ts=? WHERE id=?", (now, r["id"]))
                    st.failed += 1
                else:
                    st.pending += 1
                continue
            reasons: list[str] = []
            details: list[str] = []
            liq_then = r["liq_then"]
            change = None
            if liq_then and liq_now is not None and liq_then > 0:
                change = (liq_now / float(liq_then) - 1) * 100
                if change <= -self.liq_drop_pct and not self._already_warned(int(r["alert_id"]), "LIQUIDITY_DROP"):
                    reasons.append("LIQUIDITY_DROP")
                    details.append(f"liquidity {float(liq_then):,.0f} -> {liq_now:,.0f} ({change:+.0f}%)")
            then_v = r["safety_then"]
            if safety_now == "UNSAFE" and then_v != "UNSAFE" and not self._already_warned(int(r["alert_id"]), "SAFETY_FLIP"):
                reasons.append("SAFETY_FLIP")
                details.append(f"safety {then_v or '?'} -> UNSAFE: {', '.join(safety_reasons) or 'hard check failed'}")
            warned = 1 if reasons else 0
            self.conn.execute(
                "UPDATE rug_checks SET status='done', done_ts=?, liq_now=?, liq_change_pct=?, safety_now=?, warned=?, "
                "reason=?, detail=? WHERE id=?",
                (now, liq_now, change, safety_now, warned, "+".join(reasons) or None, "; ".join(details) or None, r["id"]))
            st.done += 1
            st.warnings += warned
            if warned:
                log.warning("RUG WARNING %s %s (+%d min after alert %d): %s", r["chain"], r["address"][:8], r["minute"],
                            r["alert_id"], "; ".join(details))
        return st

    def pending_warnings(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM rug_checks WHERE warned=1 AND delivered_ts IS NULL ORDER BY done_ts").fetchall()

    def mark_delivered(self, check_id: int, now: int | None = None) -> None:
        self.conn.execute("UPDATE rug_checks SET delivered_ts=? WHERE id=?", (int(self._clock()) if now is None else now, check_id))
