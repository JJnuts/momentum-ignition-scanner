"""Stage 1 - ignition features and WATCH nomination over Stage-0 rows.

Two feature modes, chosen per row by what the data rail provides:
  short  : row has 1m/5m/30m windows (Solana).   rVol_1m, rVol_5m, price bands,
           participation counts, impact efficiency, percentile + cohort ranks.
  hourly : row has only 1h+ windows (Robinhood). Ignition from the DELTA of
           vol_1h / trade_1h_count between consecutive polls, with a fallback on
           volume_1h_change_percent when no previous poll exists.

Every threshold comes from config.json -> chains.<name>.stage1.
Everything here is a pure function of (rows at time `now`, DB rows with ts < now),
so a replay over stored scan_rows reproduces live decisions exactly.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .config import ChainConfig
from .stage0 import ROW_COLUMNS, TokenRow

log = logging.getLogger("stage1")

RVOL_CAP = 99.0  # rVol when the baseline is zero but current volume is positive ("from nothing")

AGE_BUCKETS: list[tuple[float, str]] = [(900, "lt15m"), (7200, "15m-2h"), (86400, "2h-24h"), (math.inf, "gt24h")]
MCAP_BUCKETS: list[tuple[float, str]] = [(100_000, "20k-100k"), (1_000_000, "100k-1M"), (math.inf, "gt1M")]


def age_bucket(age_s: int | None) -> str:
    if age_s is None:
        return "unknown"
    for limit, name in AGE_BUCKETS:
        if age_s < limit:
            return name
    return "gt24h"


def mcap_bucket(mcap: float | None) -> str:
    if mcap is None:
        return "unknown"
    for limit, name in MCAP_BUCKETS:
        if mcap < limit:
            return name
    return "gt1M"


def _div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b <= 0:
        return None
    return a / b


def base_rate(window_total: float | None, current: float | None, window_s: int, unit_s: int,
              age_s: int | None) -> float | None:
    """Average volume per `unit_s` over the trailing window EXCLUDING the current unit.

    window_total: e.g. vol_1h; current: e.g. vol_5m; window_s=3600; unit_s=300.
    A token younger than the window only has `age_s` of history, so the window shrinks
    to its age. Needs at least one full prior unit of history, else None.
    """
    if window_total is None or current is None:
        return None
    eff_window = window_s if age_s is None else min(window_s, age_s)
    trailing = eff_window - unit_s
    if trailing < unit_s:
        return None
    return max(0.0, window_total - current) / (trailing / unit_s)


def rvol(current: float | None, base: float | None) -> float | None:
    if current is None or base is None:
        return None
    if base <= 0:
        return RVOL_CAP if current > 0 else None
    return min(RVOL_CAP, current / base)


def pct_rank(values: list[float], x: float) -> float | None:
    """Percentile rank of x within values (0..100, higher = larger). None if < 2 values."""
    n = len(values)
    if n < 2:
        return None
    below = sum(1 for v in values if v < x)
    return 100.0 * below / (n - 1)


def cohort_z(x: float | None, cohort: list[float], fallback: list[float], min_n: int) -> tuple[float | None, int]:
    """z-score of x within its cohort (n >= min_n) else within the fallback population."""
    if x is None:
        return None, 0
    pool = cohort if len(cohort) >= min_n else fallback
    if len(pool) < 3:
        return None, len(pool)
    mu = statistics.fmean(pool)
    sd = statistics.pstdev(pool)
    if sd <= 1e-12:
        return 0.0, len(pool)
    return (x - mu) / sd, len(pool)


@dataclass
class Features:
    mode: str
    age_s: int | None = None
    age_bucket: str = "unknown"
    mcap_bucket: str = "unknown"
    # short mode
    base_1m: float | None = None
    base_5m: float | None = None
    rvol_1m: float | None = None
    rvol_5m: float | None = None
    turnover_5m: float | None = None
    eff_5m: float | None = None
    ln_vol_liq: float | None = None
    holder_prev: int | None = None
    holder_growth_pct: float | None = None
    rvol_1m_pct: float | None = None
    tr_5m_pct: float | None = None
    eff_5m_pct: float | None = None
    # hourly mode
    prev_ts: int | None = None
    prev_dt_s: int | None = None
    d_vol_1h: float | None = None
    d_tr_1h: int | None = None
    base_dt: float | None = None
    rvol_dt: float | None = None
    turnover_1h: float | None = None
    rvol_dt_pct: float | None = None
    # ranker
    cohort_z: float | None = None
    cohort_n: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Gate:
    name: str
    passed: bool
    value: Any = None
    limit: Any = None
    kind: str = "hard"   # hard | veto | soft


@dataclass
class Evaluation:
    row: TokenRow
    features: Features
    gates: list[Gate]
    passed: bool
    fail_reasons: list[str] = field(default_factory=list)

    def gates_dict(self) -> dict[str, Any]:
        return {g.name: {"pass": g.passed, "value": g.value, "limit": g.limit, "kind": g.kind} for g in self.gates}


@dataclass
class Stage1Stats:
    chain: str
    cycle_id: int
    ts: int
    evaluated: int = 0
    passed: int = 0
    nominated: int = 0
    cooldown: int = 0
    fail_counts: Counter = field(default_factory=Counter)
    nominees: list[tuple[str, str]] = field(default_factory=list)   # (symbol, address)
    nominated_rows: list[tuple[int | None, "TokenRow"]] = field(default_factory=list, repr=False)  # (nomination id, row)
    evals: list["Evaluation"] = field(default_factory=list, repr=False)


def _row_from_db(r: sqlite3.Row) -> TokenRow:
    return TokenRow(**{c: r[c] for c in ROW_COLUMNS})


class Stage1:
    def __init__(self, conn: sqlite3.Connection, persist: bool = True,
                 clock: Callable[[], float] = time.time) -> None:
        self.conn = conn
        self.persist = persist
        self._clock = clock
        self.last_nominated: dict[tuple[str, str], int] = {}
        if persist:
            for r in conn.execute("SELECT chain, address, MAX(ts) AS ts FROM nominations "
                                  "WHERE ts >= ? GROUP BY chain, address", (int(clock()) - 86400,)):
                self.last_nominated[(r["chain"], r["address"])] = int(r["ts"])

    # ---- DB lookups (always ts < now: no lookahead) --------------------------
    def _rows_between(self, chain: str, t_from: int, t_to_exclusive: int) -> list[TokenRow]:
        cur = self.conn.execute(
            f"SELECT {', '.join(ROW_COLUMNS)} FROM scan_rows WHERE chain=? AND ts>=? AND ts<? ORDER BY ts",
            (chain, t_from, t_to_exclusive))
        return [_row_from_db(r) for r in cur.fetchall()]

    @staticmethod
    def _closest_prev(rows: list[TokenRow], target_ts: int) -> dict[str, TokenRow]:
        best: dict[str, TokenRow] = {}
        for r in rows:
            cur = best.get(r.address)
            if cur is None or abs(r.ts - target_ts) < abs(cur.ts - target_ts):
                best[r.address] = r
        return best

    # ---- feature computation ----------------------------------------------
    @staticmethod
    def mode_for(row: TokenRow) -> str:
        return "short" if row.vol_5m is not None and row.vol_1m is not None else "hourly"

    def compute_features(self, ch: ChainConfig, rows: list[TokenRow], now: int) -> list[Features]:
        s1 = ch.stage1
        if not rows:
            return []
        mode = self.mode_for(rows[0])
        feats: list[Features] = []

        if mode == "short":
            prev_map = self._closest_prev(self._rows_between(ch.name, now - 450, now - 150), now - 300)
            for r in rows:
                f = Features(mode="short", age_s=r.age_s, age_bucket=age_bucket(r.age_s), mcap_bucket=mcap_bucket(r.market_cap))
                f.base_1m = base_rate(r.vol_30m, r.vol_1m, 1800, 60, r.age_s)
                f.base_5m = base_rate(r.vol_1h, r.vol_5m, 3600, 300, r.age_s)
                f.rvol_1m = rvol(r.vol_1m, f.base_1m)
                f.rvol_5m = rvol(r.vol_5m, f.base_5m)
                f.turnover_5m = _div(r.vol_5m, r.liquidity)
                f.eff_5m = _div(r.pc_5m, f.turnover_5m) if (f.turnover_5m or 0) > 0 else None
                f.ln_vol_liq = math.log(f.turnover_5m) if (f.turnover_5m or 0) > 0 else None
                p = prev_map.get(r.address)
                if p is not None and p.holder and r.holder is not None:
                    f.holder_prev = p.holder
                    f.holder_growth_pct = 100.0 * (r.holder - p.holder) / p.holder
                feats.append(f)
            # percentile ranks within the page
            rv = [f.rvol_1m for f in feats if f.rvol_1m is not None]
            tr = [float(r.tr_5m) for r in rows if r.tr_5m is not None]
            ef = [f.eff_5m for f in feats if f.eff_5m is not None]
            for r, f in zip(rows, feats):
                f.rvol_1m_pct = pct_rank(rv, f.rvol_1m) if f.rvol_1m is not None else None
                f.tr_5m_pct = pct_rank(tr, float(r.tr_5m)) if r.tr_5m is not None else None
                f.eff_5m_pct = pct_rank(ef, f.eff_5m) if f.eff_5m is not None else None
            cohort_feature = [f.ln_vol_liq for f in feats]
        else:
            lo, hi = s1.get("prev_dt_range_s", [60, 300])
            prev_map = self._closest_prev(self._rows_between(ch.name, now - int(hi), now - int(lo) + 1), now - 120)
            for r in rows:
                f = Features(mode="hourly", age_s=r.age_s, age_bucket=age_bucket(r.age_s), mcap_bucket=mcap_bucket(r.market_cap))
                f.turnover_1h = _div(r.vol_1h, r.liquidity)
                p = prev_map.get(r.address)
                if p is not None and p.vol_1h is not None and r.vol_1h is not None:
                    f.prev_ts = p.ts
                    f.prev_dt_s = r.ts - p.ts if r.ts != p.ts else now - p.ts
                    f.d_vol_1h = r.vol_1h - p.vol_1h
                    if p.tr_1h is not None and r.tr_1h is not None:
                        f.d_tr_1h = r.tr_1h - p.tr_1h
                    f.base_dt = p.vol_1h / 3600.0 * max(1, f.prev_dt_s)
                    f.rvol_dt = rvol(max(0.0, f.d_vol_1h), f.base_dt)
                feats.append(f)
            rv = [f.rvol_dt for f in feats if f.rvol_dt is not None]
            for f in feats:
                f.rvol_dt_pct = pct_rank(rv, f.rvol_dt) if f.rvol_dt is not None else None
            cohort_feature = [math.log(f.turnover_1h) if (f.turnover_1h or 0) > 0 else None for f in feats]

        # cohort z (ranker only): current page + last 10 min of stored rows
        window = self._rows_between(ch.name, now - 600, now)
        pool: list[tuple[str, str, float]] = []
        for wr in window:
            t = _div(wr.vol_5m, wr.liquidity) if mode == "short" else _div(wr.vol_1h, wr.liquidity)
            if t and t > 0:
                pool.append((age_bucket(wr.age_s), mcap_bucket(wr.market_cap), math.log(t)))
        for r, f, x in zip(rows, feats, cohort_feature):
            if x is not None:
                pool.append((f.age_bucket, f.mcap_bucket, x))
        all_vals = [v for _, _, v in pool]
        min_n = int(s1.get("cohort_z_min_n", 12))
        for f, x in zip(feats, cohort_feature):
            coh = [v for a, m, v in pool if a == f.age_bucket and m == f.mcap_bucket]
            f.cohort_z, f.cohort_n = cohort_z(x, coh, all_vals, min_n)
        return feats

    # ---- gates ------------------------------------------------------------------
    @staticmethod
    def _ge(name: str, value: float | None, limit: float, kind: str = "hard") -> Gate:
        return Gate(name, value is not None and value >= limit, value, limit, kind)

    @staticmethod
    def _le(name: str, value: float | None, limit: float, kind: str = "hard") -> Gate:
        return Gate(name, value is not None and value <= limit, value, limit, kind)

    def gates_for(self, ch: ChainConfig, r: TokenRow, f: Features) -> list[Gate]:
        s1 = ch.stage1
        g: list[Gate] = []
        min_age = s1.get("min_age_s")
        if min_age is not None:
            # unknown age (old token) passes; known age must be >= min
            g.append(Gate("min_age", f.age_s is None or f.age_s >= int(min_age), f.age_s, min_age))
        if f.mode == "short":
            g.append(self._ge("rvol_1m", f.rvol_1m, float(s1["rvol_1m_min"])))
            g.append(self._ge("rvol_5m", f.rvol_5m, float(s1["rvol_5m_min"])))
            g.append(self._ge("trade_1m_count", r.tr_1m, float(s1["trade_1m_count_min"])))
            g.append(self._ge("trade_5m_count", r.tr_5m, float(s1["trade_5m_count_min"])))
            g.append(self._ge("price_change_5m_min", r.pc_5m, float(s1["price_change_5m_min_pct"])))
            g.append(self._le("price_change_5m_max", r.pc_5m, float(s1["price_change_5m_max_pct"]), "veto"))
            g.append(self._le("price_change_1h_max", r.pc_1h, float(s1["price_change_1h_max_pct"]), "veto"))
            g.append(self._ge("price_change_1m_min", r.pc_1m, float(s1["price_change_1m_min_pct"]), "veto"))
            # wash / impact-efficiency vetoes
            wash = (f.turnover_5m is not None and r.pc_5m is not None
                    and f.turnover_5m > float(s1["wash_turnover_5m_min"])
                    and abs(r.pc_5m) < float(s1["wash_abs_price_change_5m_max_pct"]))
            g.append(Gate("wash_turnover", not wash, f.turnover_5m, s1["wash_turnover_5m_min"], "veto"))
            neg_eff = f.eff_5m is not None and f.eff_5m < 0 and (f.rvol_5m or 0) >= float(s1["rvol_5m_min"])
            g.append(Gate("impact_eff_sign", not neg_eff, f.eff_5m, 0, "veto"))
            eff_pct_min = s1.get("impact_eff_min_percentile")
            if eff_pct_min is not None:
                g.append(self._ge("impact_eff_pct", f.eff_5m_pct, float(eff_pct_min)))
            pg = s1.get("percentile_gates") or {}
            if pg.get("rvol_1m_top_pct") is not None:
                g.append(self._ge("rvol_1m_top_pct", f.rvol_1m_pct, 100.0 - float(pg["rvol_1m_top_pct"])))
            if pg.get("trade_5m_count_top_pct") is not None:
                g.append(self._ge("trade_5m_count_top_pct", f.tr_5m_pct, 100.0 - float(pg["trade_5m_count_top_pct"])))
            hg = s1.get("holder_growth_5m_min_pct")
            if hg is not None:
                g.append(Gate("holder_growth_5m", f.holder_growth_pct is None or f.holder_growth_pct >= float(hg),
                              f.holder_growth_pct, hg, "soft"))
        else:
            have_prev = f.rvol_dt is not None
            if have_prev:
                g.append(self._ge("rvol_dt", f.rvol_dt, float(s1["rvol_dt_min"])))
                g.append(self._ge("d_trades", f.d_tr_1h, float(s1["d_trades_min"])))
            else:
                g.append(self._ge("fallback_vol_1h_chg", r.vol_1h_chg, float(s1["fallback_vol_1h_chg_min_pct"])))
                g.append(self._ge("fallback_trade_1h_count", r.tr_1h, float(s1["fallback_trade_1h_count_min"])))
                fb_max = s1.get("fallback_price_change_1h_max_pct")
                if fb_max is not None:  # a page ENTRANT already extended this much is late, not igniting
                    g.append(self._le("fallback_price_change_1h_max", r.pc_1h, float(fb_max), "veto"))
            vol_min = s1.get("vol_1h_min_usd")
            if vol_min is not None:
                g.append(self._ge("vol_1h_min", r.vol_1h, float(vol_min)))
            g.append(self._ge("price_change_1h_min", r.pc_1h, float(s1["price_change_1h_min_pct"])))
            g.append(self._le("price_change_1h_max", r.pc_1h, float(s1["price_change_1h_max_pct"]), "veto"))
            wash = (f.turnover_1h is not None and r.pc_1h is not None
                    and f.turnover_1h > float(s1["wash_turnover_1h_min"])
                    and abs(r.pc_1h) < float(s1["wash_abs_price_change_1h_max_pct"]))
            g.append(Gate("wash_turnover", not wash, f.turnover_1h, s1["wash_turnover_1h_min"], "veto"))
            pg = s1.get("percentile_gates") or {}
            if have_prev and pg.get("rvol_dt_top_pct") is not None:
                g.append(self._ge("rvol_dt_top_pct", f.rvol_dt_pct, 100.0 - float(pg["rvol_dt_top_pct"])))
        return g

    def evaluate(self, ch: ChainConfig, rows: list[TokenRow], now: int) -> list[Evaluation]:
        feats = self.compute_features(ch, rows, now)
        out: list[Evaluation] = []
        for r, f in zip(rows, feats):
            gates = self.gates_for(ch, r, f)
            fails = [g.name for g in gates if not g.passed and g.kind != "soft"]
            out.append(Evaluation(row=r, features=f, gates=gates, passed=not fails, fail_reasons=fails))
        return out

    # ---- nomination -----------------------------------------------------------------
    def run_cycle(self, ch: ChainConfig, rows: list[TokenRow], now: int, cycle_id: int) -> Stage1Stats:
        st = Stage1Stats(chain=ch.name, cycle_id=cycle_id, ts=now)
        evals = self.evaluate(ch, rows, now)
        st.evals = evals
        st.evaluated = len(evals)
        cooldown_s = int(ch.stage1.get("renominate_after_s", 1200))
        to_insert: list[tuple] = []
        insert_rows: list[TokenRow] = []
        for ev in evals:
            if not ev.passed:
                st.fail_counts.update(ev.fail_reasons[:1])   # count the FIRST failing gate only
                continue
            st.passed += 1
            key = (ch.name, ev.row.address)
            last = self.last_nominated.get(key)
            if last is not None and now - last < cooldown_s:
                st.cooldown += 1
                continue
            self.last_nominated[key] = now
            st.nominated += 1
            st.nominees.append((ev.row.symbol or "?", ev.row.address))
            to_insert.append((ch.name, ev.row.address, now, "WATCH", None, ev.row.price, ev.row.liquidity,
                              json.dumps(ev.features.to_dict(), separators=(",", ":")),
                              json.dumps(ev.gates_dict(), separators=(",", ":"), default=str)))
            insert_rows.append(ev.row)
        if self.persist and to_insert:
            self.conn.execute("BEGIN")
            try:
                for params, row in zip(to_insert, insert_rows):
                    cur = self.conn.execute(
                        "INSERT INTO nominations(chain, address, ts, tier, score, price, liquidity, features_json, gates_json) "
                        "VALUES(?,?,?,?,?,?,?,?,?)", params)
                    st.nominated_rows.append((int(cur.lastrowid), row))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        else:
            st.nominated_rows = [(None, row) for row in insert_rows]
        return st
