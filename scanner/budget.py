"""CU budget audit (T15a): `python -m scanner budget [--since-days N] [--out x.md]`.

Reads only `cu_ledger`. Per LOCAL day (scanner.clock): CU, calls, the time the
daily cap was crossed, active hours (any CU), dark hours (an hour inside the
observed span with zero CU - the scanner was up but not calling), CU per
active hour, and the projected full-day need at that pace (CU/active hour x
24). Then CU by local hour per day and CU by endpoint over the window.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from . import clock


@dataclass
class DayBudget:
    date: str
    start_ts: int
    end_ts: int             # exclusive; min(next local midnight, now)
    observed_from: int = 0  # max(day start, first ledger row ever): hours before it were never observed
    cu: int = 0
    calls: int = 0
    by_hour: list[int] = field(default_factory=lambda: [0] * 24)
    cap_hit_ts: int | None = None
    observed_hours: int = 0     # hours inside [first ledger row ever, now]
    active_hours: int = 0
    dark_hours: int = 0
    quiet_cu: int = 0           # CU spent inside the configured quiet block (T15b)
    quiet_hours_n: int = 0

    @property
    def cu_per_active_hour(self) -> float:
        return self.cu / self.active_hours if self.active_hours else 0.0

    @property
    def projected_24h(self) -> int:
        return int(round(self.cu_per_active_hour * 24))

    @property
    def projected_with_quiet(self) -> int:
        """projected_24h minus what the quiet block would have saved at the same pace."""
        if not self.active_hours or not self.quiet_hours_n:
            return self.projected_24h
        return int(round(self.cu_per_active_hour * (24 - self.quiet_hours_n)))


@dataclass
class BudgetReport:
    tz: str
    daily_cap: int
    now: int
    days: list[DayBudget]
    by_endpoint: list[tuple[str, int, int]]      # endpoint, calls, cu

    @property
    def dark_hours_total(self) -> int:
        return sum(d.dark_hours for d in self.days)


def build(conn: sqlite3.Connection, daily_cap: int, days: int = 3, now: int | None = None,
          quiet_hours: set[int] | None = None) -> BudgetReport:
    now = int(time.time()) if now is None else int(now)
    days = max(1, int(days))
    first_row = conn.execute("SELECT MIN(ts) FROM cu_ledger").fetchone()[0]
    first_ts = int(first_row) if first_row is not None else now
    # local-day windows, oldest first
    starts: list[int] = []
    s = clock.day_start(now)
    for _ in range(days):
        starts.append(s)
        s = clock.day_start(s - 1)
    starts.reverse()
    out: list[DayBudget] = []
    for start in starts:
        end = min(clock.next_day_start(start), now)
        if end <= start:
            continue
        db = DayBudget(date=clock.local_date(start), start_ts=start, end_ts=end, quiet_hours_n=len(quiet_hours or ()))
        rows = conn.execute("SELECT ts, cu FROM cu_ledger WHERE ts >= ? AND ts < ? ORDER BY ts", (start, end)).fetchall()
        running = 0
        for ts, cu in rows:
            ts, cu = int(ts), int(cu)
            db.cu += cu
            db.calls += 1
            hh = clock.local_hour(ts)
            db.by_hour[hh] += cu
            if quiet_hours and hh in quiet_hours:
                db.quiet_cu += cu
            running += cu
            if db.cap_hit_ts is None and daily_cap > 0 and running >= daily_cap:
                db.cap_hit_ts = ts
        # hour h is observed when it overlaps [max(start, first_ts), end)
        obs_lo = max(start, first_ts)
        db.observed_from = obs_lo
        for h in range(24):
            h_start = start + h * 3600
            h_end = h_start + 3600
            if h_end <= obs_lo or h_start >= end:
                continue
            db.observed_hours += 1
            if db.by_hour[h] > 0:
                db.active_hours += 1
            else:
                db.dark_hours += 1
        out.append(db)
    win_start = starts[0]
    by_ep = conn.execute("SELECT endpoint, COUNT(*), COALESCE(SUM(cu),0) FROM cu_ledger WHERE ts >= ? AND ts < ? "
                         "GROUP BY endpoint ORDER BY 3 DESC", (win_start, now)).fetchall()
    return BudgetReport(tz=clock.tz_name(), daily_cap=int(daily_cap), now=now, days=out,
                        by_endpoint=[(str(e), int(n), int(c)) for e, n, c in by_ep])


def format_report(r: BudgetReport) -> str:
    L: list[str] = []
    L.append(f"# budget report (tz {r.tz}, daily cap {r.daily_cap:,} CU, as of {clock.local_date(r.now)} {clock.local_hms(r.now)})")
    L.append("")
    L.append("## per local day")
    L.append("| day | CU | calls | cap hit at | active h | dark h | CU / active h | projected 24h | quiet-block CU | projected with quiet | verdict |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for d in r.days:
        hit = clock.local_hms(d.cap_hit_ts) if d.cap_hit_ts else "-"
        verdict = "OVER cap" if d.projected_24h > r.daily_cap else "within cap"
        if d.dark_hours:
            verdict += f", {d.dark_hours} dark h"
        L.append(f"| {d.date} | {d.cu:,} | {d.calls:,} | {hit} | {d.active_hours}/{d.observed_hours} | {d.dark_hours} "
                 f"| {d.cu_per_active_hour:,.0f} | {d.projected_24h:,} | {d.quiet_cu:,} | {d.projected_with_quiet:,} | {verdict} |")
    active_days = [d for d in r.days if d.active_hours]
    if active_days:
        avg = sum(d.projected_24h for d in active_days) / len(active_days)
        L.append("")
        L.append(f"projected full-day need (mean of {len(active_days)} active days): {avg:,.0f} CU vs cap {r.daily_cap:,} "
                 f"({'OVER by ' + format(avg - r.daily_cap, ',.0f') if avg > r.daily_cap else 'within'}); "
                 f"dark hours total: {r.dark_hours_total}")
        if any(d.quiet_hours_n for d in active_days):
            saving = sum(d.projected_24h - d.projected_with_quiet for d in active_days) / len(active_days)
            with_q = sum(d.projected_with_quiet for d in active_days) / len(active_days)
            L.append(f"quiet block saves {saving:,.0f} CU/day at the same pace -> projected {with_q:,.0f} "
                     f"({'still OVER by ' + format(with_q - r.daily_cap, ',.0f') if with_q > r.daily_cap else 'within cap'})")
    L.append("")
    L.append("## CU by local hour")
    L.append("| hour | " + " | ".join(d.date for d in r.days) + " |")
    L.append("|---|" + "---|" * len(r.days))
    for h in range(24):
        cells = []
        for d in r.days:
            h_start = d.start_ts + h * 3600
            if h_start >= d.end_ts or h_start + 3600 <= d.observed_from:
                cells.append("")                       # not observed (future, or before the first ledger row)
            else:
                v = d.by_hour[h]
                cells.append(f"{v:,}" if v else "DARK")
        L.append(f"| {h:02d} | " + " | ".join(cells) + " |")
    L.append("")
    L.append("## CU by endpoint (window)")
    L.append("| endpoint | calls | CU | share |")
    L.append("|---|---|---|---|")
    total = sum(c for _, _, c in r.by_endpoint) or 1
    for ep, n, c in r.by_endpoint:
        L.append(f"| {ep} | {n:,} | {c:,} | {100 * c / total:.0f}% |")
    L.append("")
    L.append("definitions: local day = midnight-to-midnight in the configured tz. 'cap hit at' = when the local-day "
             "running total crossed the cap (days before the T15a relaunch were governed by UTC days, so their spend "
             "continued past this time). DARK = an hour with zero CU inside the observed span [first ledger row, now]; "
             "the ledger cannot tell 'capped' from 'process not running'. projected 24h = CU per active hour x 24.")
    return "\n".join(L)
