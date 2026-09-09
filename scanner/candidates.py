"""Candidate manager (T8) - the active set that Stage 2 spends CU on.

Lifecycle (SPEC s10 hysteresis):
  ENTER    a Stage-1 nomination (full trigger) adds the token, or refreshes it.
  STAY     while active it only needs the weaker condition: Stage-1 rVol_5m
           (or rvol_dt) >= stay_rvol, tape OFI30 >= stay_ofi. Failing the
           stay check does not evict immediately; silence does (max_stay_min
           since last evidence).
  CAP      max_per_chain; when full, a stronger newcomer evicts the weakest
           (lowest strength, then oldest). Weaker newcomers are rejected.
  VETO     a hard veto from Stage 2 removes the token and blocks re-entry for
           veto_cooldown_min.
  DEGRADE  when the day's CU is at the cap, active() is empty (no deep polls;
           Stage 0/1 keep running = WATCH-only) until the day rolls over.

State is persisted in `candidates` so a restart resumes the same set.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import ChainConfig
from .ledger import CULedger

log = logging.getLogger("candidates")


@dataclass
class Candidate:
    chain: str
    address: str
    symbol: str | None
    first_seen_ts: int
    last_seen_ts: int          # last evidence (nomination or stay pass)
    nominated_ts: int
    strength: float            # eviction ranking (higher = keep)
    s1_rvol: float | None = None
    ofi30: float | None = None
    anchor_ts: int | None = None
    anchor_price: float | None = None
    polls: int = 0
    stay_fails: int = 0
    status: str = "active"     # active | evicted | expired | vetoed | rejected

    def state(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "nominated_ts": self.nominated_ts, "strength": self.strength,
                "s1_rvol": self.s1_rvol, "ofi30": self.ofi30, "polls": self.polls, "stay_fails": self.stay_fails}


@dataclass
class ManagerStats:
    entered: int = 0
    refreshed: int = 0
    rejected_cap: int = 0
    evicted: int = 0
    expired: int = 0
    vetoed: int = 0
    blocked_cooldown: int = 0
    degraded_polls_skipped: int = 0
    events: list[str] = field(default_factory=list)


class CandidateManager:
    def __init__(self, conn: sqlite3.Connection, ledger: CULedger | None, settings: dict[str, Any],
                 daily_cu_cap: int, clock: Callable[[], float] = time.time) -> None:
        self.conn = conn
        self.ledger = ledger
        self.daily_cu_cap = daily_cu_cap
        self._clock = clock
        self.max_per_chain = int(settings.get("max_per_chain", 12))
        self.max_stay_s = int(settings.get("max_stay_min", 8)) * 60
        self.stay_rvol = float(settings.get("stay_rvol_5m_min", 2.0))
        self.stay_ofi = float(settings.get("stay_ofi_min", settings.get("stay_ofi_5m_min", 0.0)))
        self.veto_cooldown_s = int(settings.get("veto_cooldown_min", 20)) * 60
        self.active_set: dict[tuple[str, str], Candidate] = {}
        self.veto_until: dict[tuple[str, str], int] = {}
        self.stats = ManagerStats()
        self.degraded_day: int | None = None
        self._load()

    # ---- persistence ----------------------------------------------------------------
    def _load(self) -> None:
        now = int(self._clock())
        for r in self.conn.execute("SELECT chain, address, first_seen_ts, last_seen_ts, anchor_ts, anchor_price, "
                                   "state_json FROM candidates WHERE status='active'"):
            if now - int(r["last_seen_ts"]) > self.max_stay_s:
                self.conn.execute("UPDATE candidates SET status='expired' WHERE chain=? AND address=?",
                                  (r["chain"], r["address"]))
                continue
            st = json.loads(r["state_json"] or "{}")
            self.active_set[(r["chain"], r["address"])] = Candidate(
                chain=r["chain"], address=r["address"], symbol=st.get("symbol"), first_seen_ts=int(r["first_seen_ts"]),
                last_seen_ts=int(r["last_seen_ts"]), nominated_ts=int(st.get("nominated_ts") or r["first_seen_ts"]),
                strength=float(st.get("strength") or 0.0), s1_rvol=st.get("s1_rvol"), ofi30=st.get("ofi30"),
                anchor_ts=r["anchor_ts"], anchor_price=r["anchor_price"], polls=int(st.get("polls") or 0),
                stay_fails=int(st.get("stay_fails") or 0))
        for r in self.conn.execute("SELECT chain, address, last_seen_ts FROM candidates WHERE status='vetoed' AND last_seen_ts>=?",
                                   (now - self.veto_cooldown_s,)):
            self.veto_until[(r["chain"], r["address"])] = int(r["last_seen_ts"]) + self.veto_cooldown_s

    def _save(self, c: Candidate) -> None:
        self.conn.execute(
            "INSERT INTO candidates(chain, address, first_seen_ts, last_seen_ts, anchor_ts, anchor_price, status, state_json) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(chain, address) DO UPDATE SET first_seen_ts=excluded.first_seen_ts, "
            "last_seen_ts=excluded.last_seen_ts, anchor_ts=excluded.anchor_ts, anchor_price=excluded.anchor_price, "
            "status=excluded.status, state_json=excluded.state_json",
            (c.chain, c.address, c.first_seen_ts, c.last_seen_ts, c.anchor_ts, c.anchor_price, c.status,
             json.dumps(c.state(), separators=(",", ":"), default=str)))

    def _retire(self, c: Candidate, status: str, now: int) -> None:
        c.status = status
        c.last_seen_ts = now
        self._save(c)
        self.active_set.pop((c.chain, c.address), None)

    # ---- lifecycle -------------------------------------------------------------------------
    @staticmethod
    def strength_of(s1_rvol: float | None, ofi30: float | None) -> float:
        base = float(s1_rvol) if s1_rvol is not None else 1.0
        return base * (1.0 + max(-0.9, float(ofi30) if ofi30 is not None else 0.0))

    def on_nomination(self, ch: ChainConfig, address: str, symbol: str | None, s1_rvol: float | None,
                      now: int, anchor_ts: int | None = None, anchor_price: float | None = None) -> str:
        """Returns one of: entered | refreshed | rejected_cap | blocked_cooldown."""
        key = (ch.name, address)
        if self.veto_until.get(key, 0) > now:
            self.stats.blocked_cooldown += 1
            return "blocked_cooldown"
        strength = self.strength_of(s1_rvol, None)
        c = self.active_set.get(key)
        if c is not None:
            c.last_seen_ts = now
            c.nominated_ts = now
            c.s1_rvol = s1_rvol
            c.strength = max(c.strength, self.strength_of(s1_rvol, c.ofi30))
            self._save(c)
            self.stats.refreshed += 1
            return "refreshed"
        chain_active = [x for x in self.active_set.values() if x.chain == ch.name]
        if len(chain_active) >= self.max_per_chain:
            weakest = min(chain_active, key=lambda x: (x.strength, x.first_seen_ts))   # weakest, then OLDEST
            if weakest.strength >= strength:
                self.stats.rejected_cap += 1
                return "rejected_cap"
            self._retire(weakest, "evicted", now)
            self.stats.evicted += 1
            self.stats.events.append(f"evict {ch.name} {weakest.symbol or weakest.address[:8]} "
                                     f"(str {weakest.strength:.2f}) for {symbol or address[:8]} ({strength:.2f})")
        c = Candidate(chain=ch.name, address=address, symbol=symbol, first_seen_ts=now, last_seen_ts=now,
                      nominated_ts=now, strength=strength, s1_rvol=s1_rvol,
                      anchor_ts=anchor_ts or now, anchor_price=anchor_price)
        self.active_set[key] = c
        self._save(c)
        self.stats.entered += 1
        return "entered"

    def on_stage1_row(self, ch: ChainConfig, address: str, rvol: float | None, now: int) -> None:
        """STAY check from a Stage-1 page row of an active candidate."""
        c = self.active_set.get((ch.name, address))
        if c is None:
            return
        if rvol is not None and rvol >= self.stay_rvol:
            c.last_seen_ts = now
            c.stay_fails = 0
        else:
            c.stay_fails += 1
        self._save(c)

    def on_tape(self, ch: ChainConfig, address: str, ofi30: float | None, hard_vetoes: list[str], now: int,
                anchor_ts: int | None = None, anchor_price: float | None = None) -> str | None:
        """Update from Stage-2 features. Returns 'vetoed' if removed."""
        c = self.active_set.get((ch.name, address))
        if c is None:
            return None
        c.polls += 1
        c.ofi30 = ofi30
        if anchor_ts:
            c.anchor_ts, c.anchor_price = anchor_ts, anchor_price
        c.strength = self.strength_of(c.s1_rvol, ofi30)
        if hard_vetoes:
            self._retire(c, "vetoed", now)
            self.veto_until[(ch.name, address)] = now + self.veto_cooldown_s
            self.stats.vetoed += 1
            self.stats.events.append(f"veto {ch.name} {c.symbol or address[:8]}: {','.join(hard_vetoes)}")
            return "vetoed"
        if ofi30 is not None and ofi30 >= self.stay_ofi:
            c.last_seen_ts = now
            c.stay_fails = 0
        else:
            c.stay_fails += 1
        self._save(c)
        return None

    def expire(self, now: int) -> list[Candidate]:
        gone = [c for c in list(self.active_set.values()) if now - c.last_seen_ts > self.max_stay_s]
        for c in gone:
            self._retire(c, "expired", now)
            self.stats.expired += 1
        return gone

    # ---- budget degrade ------------------------------------------------------------------
    def degraded(self, now: int | None = None) -> bool:
        now = int(self._clock()) if now is None else now
        day = now // 86400
        if self.degraded_day == day:
            return True
        if self.ledger is not None and self.ledger.total_since(day * 86400) >= self.daily_cu_cap:   # 'today' by OUR clock
            self.degraded_day = day
            log.warning("DAILY CU CAP REACHED (%d) - degrading to WATCH-only (no deep polls) until the day rolls over",
                        self.daily_cu_cap)
            return True
        return False

    def active(self, ch: ChainConfig | None = None, now: int | None = None) -> list[Candidate]:
        now = int(self._clock()) if now is None else now
        if self.degraded(now):
            self.stats.degraded_polls_skipped += 1
            return []
        items = [c for c in self.active_set.values() if ch is None or c.chain == ch.name]
        return sorted(items, key=lambda c: (-c.strength, c.first_seen_ts))
