"""Replay Stage 1 over stored Stage-0 rows (no API calls, nothing persisted).

Walks every (chain, cycle) in scan_rows in time order and evaluates Stage 1
exactly as the live loop would have at that moment (all lookups are ts < now).
Reports nomination rate per hour, the first failing gate distribution, and
the nominees, so thresholds can be calibrated ONCE before freezing.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from .config import ChainConfig, Config
from .db import open_db
from .stage0 import ROW_COLUMNS
from .stage1 import Stage1, _row_from_db


@dataclass
class ReplayChain:
    chain: str
    cycles: int = 0
    first_ts: int | None = None
    last_ts: int | None = None
    evaluated: int = 0
    passed: int = 0
    nominated: int = 0
    cooldown: int = 0
    fail_counts: Counter = field(default_factory=Counter)
    nominees: list[tuple[int, str, str, dict]] = field(default_factory=list)  # (ts, symbol, address, key features)

    @property
    def hours(self) -> float:
        if self.first_ts is None or self.last_ts is None:
            return 0.0
        return max(1 / 60, (self.last_ts - self.first_ts) / 3600 + 1 / 60)  # add one cycle of width


def replay(cfg: Config, conn: sqlite3.Connection | None = None, chains: list[str] | None = None,
           since_ts: int | None = None) -> dict[str, ReplayChain]:
    own = conn is None
    conn = conn or open_db(cfg.db_path)
    stage1 = Stage1(conn, persist=False)
    out: dict[str, ReplayChain] = {}
    for ch in cfg.enabled_chains:
        if chains and ch.name not in chains:
            continue
        rc = ReplayChain(chain=ch.name)
        cycles = conn.execute(
            "SELECT cycle_id, ts FROM scan_rows WHERE chain=? AND ts>=? GROUP BY cycle_id, ts ORDER BY ts, cycle_id",
            (ch.name, since_ts or 0)).fetchall()
        for c in cycles:
            rows = [_row_from_db(r) for r in conn.execute(
                f"SELECT {', '.join(ROW_COLUMNS)} FROM scan_rows WHERE chain=? AND cycle_id=? ORDER BY rank",
                (ch.name, c["cycle_id"]))]
            st = stage1.run_cycle(ch, rows, int(c["ts"]), int(c["cycle_id"]))
            rc.cycles += 1
            rc.first_ts = rc.first_ts if rc.first_ts is not None else int(c["ts"])
            rc.last_ts = int(c["ts"])
            rc.evaluated += st.evaluated
            rc.passed += st.passed
            rc.nominated += st.nominated
            rc.cooldown += st.cooldown
            rc.fail_counts.update(st.fail_counts)
            if st.nominees:
                evals = {e.row.address: e for e in stage1.evaluate(ch, rows, int(c["ts"]))}
                for sym, addr in st.nominees:
                    f = evals[addr].features
                    r = evals[addr].row
                    key = ({"mcap": r.market_cap, "liq": r.liquidity, "age_s": f.age_s,
                            "rvol_1m": f.rvol_1m, "rvol_5m": f.rvol_5m, "pc_5m": r.pc_5m, "tr_5m": r.tr_5m,
                            "eff_pct": f.eff_5m_pct, "z": f.cohort_z}
                           if f.mode == "short" else
                           {"mcap": r.market_cap, "liq": r.liquidity, "age_s": f.age_s, "rvol_dt": f.rvol_dt,
                            "d_tr": f.d_tr_1h, "pc_1h": r.pc_1h, "vol_1h_chg": r.vol_1h_chg, "z": f.cohort_z})
                    rc.nominees.append((int(c["ts"]), sym, addr, key))
        out[ch.name] = rc
    if own:
        conn.close()
    return out


def format_replay(res: dict[str, ReplayChain]) -> str:
    lines = ["replay (Stage 1 over stored scan_rows; nothing persisted)"]
    for name, rc in res.items():
        lines.append(f"  {name:<10} cycles={rc.cycles} span={rc.hours:.2f}h evaluated={rc.evaluated} "
                     f"passed={rc.passed} nominated={rc.nominated} -> {rc.nominated / rc.hours if rc.hours else 0:.1f}/h "
                     f"cooldown={rc.cooldown}")
        total_fail = sum(rc.fail_counts.values())
        for gate, n in rc.fail_counts.most_common(8):
            lines.append(f"             first-fail {gate:<26} {n:>6}  ({100 * n / total_fail:.0f}%)")
        for ts, sym, addr, key in rc.nominees[:20]:
            kf = " ".join(f"{k}={_fmt(v)}" for k, v in key.items())
            lines.append(f"             {ts} {sym:<10} {addr[:10]}..  {kf}")
    return "\n".join(lines)


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.2f}" if abs(v) < 1000 else f"{v:.0f}"
    return str(v)
