"""Labeler v1 - forward outcome snapshots for nominations and control samples.

For every nomination (and a random CONTROL sample of eligible rows each cycle)
we create one `labels` row per horizon (default +5/+15/+30/+60 min). A
background tick then fills them in two passes:

  1. CLOSE pass (every horizon, cheap): price + liquidity at the due time.
     Source order: a scan_rows snapshot within +-match_window (free) ->
     multi_price batch (Lite+ plans; 1..100 addresses per call) -> retry next
     tick until `grace_s` after due, then FAILED.
  2. PATH pass (final horizon only, nominations only by default): one OHLCV 1m
     call over [t0, t0 + max horizon] fills high/low/close for ALL horizons of
     that event (MFE/MAE). 45 CU per event. Controls get close-based labels
     only, which is all the treatment-vs-control lift analysis needs.

Everything is restart-safe: pending work lives in the DB, not in memory.
"""
from __future__ import annotations

import json
import logging
import math
import random
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .birdeye import BirdeyeClient, BirdeyeError, EndpointUnavailable
from .config import ChainConfig
from .ledger import CULedger
from .plans import cu_cost, endpoint_available
from .stage0 import TokenRow
from .stage1 import Stage1Stats

log = logging.getLogger("labeler")


@dataclass
class CycleLabelStats:
    chain: str
    nominations_enqueued: int = 0
    controls_drawn: int = 0
    labels_created: int = 0
    control_ids: list[int] = field(default_factory=list)


@dataclass
class TickStats:
    due: int = 0
    done_scan_row: int = 0
    done_multi_price: int = 0
    failed: int = 0
    still_pending: int = 0
    path_due: int = 0
    path_done: int = 0
    path_failed: int = 0
    path_skipped_budget: int = 0
    cu: int = 0


class Labeler:
    def __init__(self, conn: sqlite3.Connection, client: BirdeyeClient | None, ledger: CULedger | None,
                 settings: dict[str, Any], plan: str, daily_cu_cap: int,
                 clock: Callable[[], float] = time.time) -> None:
        self.conn = conn
        self.client = client
        self.ledger = ledger
        self.plan = plan
        self.daily_cu_cap = daily_cu_cap
        self._clock = clock
        self.horizons: list[int] = [int(h) for h in settings.get("horizons_min", [5, 15, 30, 60])]
        self.control_per_cycle = float(settings.get("control_sample_per_cycle", 0.5))
        self.tick_interval_s = int(settings.get("tick_interval_s", 30))
        self.match_window_s = int(settings.get("scan_row_match_window_s", 90))
        self.grace_s = int(settings.get("grace_s", 600))
        self.path_for = str(settings.get("ohlcv_path_for", "nominations"))   # nominations | all | none
        self.max_path_attempts = int(settings.get("max_path_attempts", 3))
        self.path_deadline_s = int(settings.get("path_deadline_s", 6 * 3600))

    # ---- enqueue ------------------------------------------------------------------
    def enqueue(self, ref_kind: str, ref_id: int | None, chain: str, address: str, t0_ts: int,
                t0_price: float | None, t0_liq: float | None) -> list[int]:
        ids: list[int] = []
        for h in self.horizons:
            cur = self.conn.execute(
                "INSERT INTO labels(ref_kind, ref_id, chain, address, t0_ts, t0_price, t0_liq, horizon_min, due_ts, status) "
                "VALUES(?,?,?,?,?,?,?,?,?,'pending')",
                (ref_kind, ref_id, chain, address, t0_ts, t0_price, t0_liq, h, t0_ts + h * 60))
            ids.append(int(cur.lastrowid))
        return ids

    def on_cycle(self, ch: ChainConfig, s1: Stage1Stats, now: int, cycle_id: int) -> CycleLabelStats:
        st = CycleLabelStats(chain=ch.name)
        self.conn.execute("BEGIN")
        try:
            nominated_addrs: set[str] = set()
            for nom_id, row in s1.nominated_rows:
                nominated_addrs.add(row.address)
                st.labels_created += len(self.enqueue("nomination", nom_id, ch.name, row.address, now,
                                                      row.price, row.liquidity))
                st.nominations_enqueued += 1
            # control sample: eligible rows that were NOT nominated this cycle, drawn with a seeded RNG
            pool = [ev for ev in s1.evals if ev.row.address not in nominated_addrs and ev.row.price]
            k = self._draw_count(ch.name, cycle_id)
            if pool and k > 0:
                rng = random.Random(f"{ch.name}:{cycle_id}:control")
                for ev in rng.sample(pool, min(k, len(pool))):
                    cur = self.conn.execute(
                        "INSERT INTO nominations(chain, address, ts, tier, score, price, liquidity, features_json, gates_json) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (ch.name, ev.row.address, now, "CONTROL", None, ev.row.price, ev.row.liquidity,
                         json.dumps(ev.features.to_dict(), separators=(",", ":")),
                         json.dumps(ev.gates_dict(), separators=(",", ":"), default=str)))
                    cid = int(cur.lastrowid)
                    st.control_ids.append(cid)
                    st.labels_created += len(self.enqueue("control", cid, ch.name, ev.row.address, now,
                                                          ev.row.price, ev.row.liquidity))
                    st.controls_drawn += 1
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return st

    def _draw_count(self, chain: str, cycle_id: int) -> int:
        whole = math.floor(self.control_per_cycle)
        frac = self.control_per_cycle - whole
        rng = random.Random(f"{chain}:{cycle_id}:count")
        return whole + (1 if rng.random() < frac else 0)

    # ---- snapshot sources -------------------------------------------------------------
    def _scan_row_price(self, chain: str, address: str, at_ts: int) -> tuple[float, float | None] | None:
        r = self.conn.execute(
            "SELECT price, liquidity FROM scan_rows WHERE chain=? AND address=? AND ts BETWEEN ? AND ? "
            "AND price IS NOT NULL ORDER BY ABS(ts - ?) LIMIT 1",
            (chain, address, at_ts - self.match_window_s, at_ts + self.match_window_s, at_ts)).fetchone()
        if r is None:
            return None
        return float(r["price"]), (float(r["liquidity"]) if r["liquidity"] is not None else None)

    def _budget_ok(self, endpoint: str, **size) -> bool:
        if self.ledger is None:
            return True
        cu, _ = cu_cost(endpoint, **size)
        return self.ledger.today_total() + cu <= self.daily_cu_cap

    async def _multi_price(self, birdeye_chain: str, addresses: list[str]) -> dict[str, tuple[float, float | None]]:
        out: dict[str, tuple[float, float | None]] = {}
        if self.client is None or not addresses or not endpoint_available("multi_price", self.plan):
            return out
        if not self._budget_ok("multi_price", n=len(addresses)):
            log.warning("multi_price skipped: daily CU cap")
            return out
        try:
            data = await self.client.multi_price(birdeye_chain, addresses[:100], include_liquidity=True)
        except EndpointUnavailable:
            return out
        except BirdeyeError as e:
            log.warning("multi_price failed: %s", e)
            return out
        for addr, v in (data or {}).items():
            if isinstance(v, dict) and v.get("value") is not None:
                liq = v.get("liquidity")
                out[addr] = (float(v["value"]), float(liq) if liq is not None else None)
        return out

    # ---- tick -------------------------------------------------------------------------------
    async def tick(self, chains: dict[str, ChainConfig], now: int | None = None) -> TickStats:
        now = int(self._clock()) if now is None else int(now)
        st = TickStats()
        st.cu += await self._close_pass(chains, now, st)
        st.cu += await self._path_pass(chains, now, st)
        return st

    async def _close_pass(self, chains: dict[str, ChainConfig], now: int, st: TickStats) -> int:
        cu = 0
        due = self.conn.execute(
            "SELECT id, chain, address, due_ts FROM labels WHERE status='pending' AND due_ts<=? ORDER BY due_ts",
            (now,)).fetchall()
        st.due = len(due)
        if not due:
            return 0
        by_chain: dict[str, list[sqlite3.Row]] = {}
        for r in due:
            by_chain.setdefault(r["chain"], []).append(r)
        for chain, items in by_chain.items():
            unresolved: list[sqlite3.Row] = []
            for r in items:
                hit = self._scan_row_price(chain, r["address"], int(r["due_ts"]))
                if hit:
                    self._mark_done(int(r["id"]), now, hit[0], hit[1], "scan_row")
                    st.done_scan_row += 1
                else:
                    unresolved.append(r)
            if unresolved and chain in chains:
                addrs = sorted({r["address"] for r in unresolved})
                cu_before = self.ledger.session_cu if self.ledger else 0
                prices = await self._multi_price(chains[chain].birdeye_chain, addrs)
                cu += (self.ledger.session_cu - cu_before) if self.ledger else 0
                for r in unresolved:
                    hit = prices.get(r["address"])
                    if hit:
                        self._mark_done(int(r["id"]), now, hit[0], hit[1], "multi_price")
                        st.done_multi_price += 1
                    elif now - int(r["due_ts"]) > self.grace_s:
                        self.conn.execute("UPDATE labels SET status='failed', done_ts=?, attempts=attempts+1 WHERE id=?",
                                          (now, int(r["id"])))
                        st.failed += 1
                    else:
                        self.conn.execute("UPDATE labels SET attempts=attempts+1 WHERE id=?", (int(r["id"]),))
                        st.still_pending += 1
            elif unresolved:
                st.still_pending += len(unresolved)
        return cu

    def _mark_done(self, label_id: int, now: int, price: float, liq: float | None, source: str) -> None:
        self.conn.execute(
            "UPDATE labels SET status='done', done_ts=?, price=?, liquidity=COALESCE(?, liquidity), source=? WHERE id=?",
            (now, price, liq, source, label_id))

    async def _path_pass(self, chains: dict[str, ChainConfig], now: int, st: TickStats) -> int:
        if self.path_for == "none" or self.client is None:
            return 0
        max_h = max(self.horizons)
        kinds = "('nomination')" if self.path_for == "nominations" else "('nomination','control')"
        # one event = (ref_kind, ref_id); its path is due when the final horizon has passed
        events = self.conn.execute(
            f"SELECT ref_kind, ref_id, chain, address, t0_ts, MIN(attempts) AS attempts FROM labels "
            f"WHERE path_status='pending' AND ref_kind IN {kinds} AND horizon_min=? AND due_ts<=? "
            f"GROUP BY ref_kind, ref_id, chain, address, t0_ts ORDER BY due_ts", (max_h, now)).fetchall()
        st.path_due = len(events)
        cu = 0
        for ev in events:
            chain, address, t0 = ev["chain"], ev["address"], int(ev["t0_ts"])
            where = "ref_kind=? AND ref_id=? AND chain=? AND address=?"
            args = (ev["ref_kind"], ev["ref_id"], chain, address)
            if now - (t0 + max_h * 60) > self.path_deadline_s or int(ev["attempts"]) >= self.max_path_attempts:
                self.conn.execute(f"UPDATE labels SET path_status='failed' WHERE {where}", args)
                st.path_failed += 1
                continue
            if chain not in chains:
                continue
            if not self._budget_ok("ohlcv_v3", candles=max_h + 2):
                st.path_skipped_budget += 1
                continue
            cu_before = self.ledger.session_cu if self.ledger else 0
            try:
                candles = await self.client.ohlcv_v3(chains[chain].birdeye_chain, address, "1m", t0, t0 + max_h * 60,
                                                     count_limit=max_h + 2)
            except BirdeyeError as e:
                log.warning("ohlcv path failed for %s %s: %s", chain, address[:10], e)
                self.conn.execute(f"UPDATE labels SET attempts=attempts+1 WHERE {where}", args)
                continue
            cu += (self.ledger.session_cu - cu_before) if self.ledger else 0
            if not candles:
                self.conn.execute(f"UPDATE labels SET attempts=attempts+1 WHERE {where}", args)
                continue
            self.apply_path(ev["ref_kind"], ev["ref_id"], chain, address, t0, candles)
            st.path_done += 1
        return cu

    def apply_path(self, ref_kind: str, ref_id: int | None, chain: str, address: str, t0: int,
                   candles: list[dict[str, Any]]) -> dict[int, tuple[float, float, float]]:
        """Fill high/low (and close if missing) for every horizon from 1m candles. Returns {h: (high, low, close)}."""
        cs = sorted((c for c in candles if c.get("unix_time") is not None), key=lambda c: c["unix_time"])
        out: dict[int, tuple[float, float, float]] = {}
        for h in self.horizons:
            window = [c for c in cs if t0 <= int(c["unix_time"]) < t0 + h * 60]
            if not window:
                continue
            hi = max(float(c["h"]) for c in window)
            lo = min(float(c["l"]) for c in window)
            close = float(window[-1]["c"])
            out[h] = (hi, lo, close)
            self.conn.execute(
                "UPDATE labels SET high=?, low=?, price=COALESCE(price, ?), path_status='done', "
                "status=CASE WHEN status IN ('pending','failed') THEN 'done' ELSE status END, "
                "done_ts=COALESCE(done_ts, ?), source=COALESCE(source, 'ohlcv') "
                "WHERE ref_kind=? AND ref_id IS ? AND chain=? AND address=? AND horizon_min=?",
                (hi, lo, close, int(self._clock()), ref_kind, ref_id, chain, address, h))
        # horizons with no candles at all -> path failed for those rows
        self.conn.execute(
            "UPDATE labels SET path_status='failed' WHERE ref_kind=? AND ref_id IS ? AND chain=? AND address=? "
            "AND path_status='pending'", (ref_kind, ref_id, chain, address))
        return out

    # ---- reporting ---------------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, path_status, COUNT(*) AS n FROM labels GROUP BY status, path_status").fetchall()
        out: dict[str, int] = {}
        for r in rows:
            out[f"{r['status']}/{r['path_status']}"] = int(r["n"])
        return out
