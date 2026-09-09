"""Compute-unit ledger: one row per API call, persisted to SQLite."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CallRecord:
    endpoint: str
    chain: str | None
    cu: int
    verified: bool
    status: int | None
    latency_ms: int
    params: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    charged: bool = True  # False when the call failed and we assume no CU was billed


class CULedger:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.session_cu = 0
        self.session_calls = 0

    def record(self, rec: CallRecord) -> None:
        cu = rec.cu if rec.charged else 0
        self.conn.execute(
            "INSERT INTO cu_ledger(ts, chain, endpoint, cu, verified, status, latency_ms) "
            "VALUES(?,?,?,?,?,?,?)",
            (int(rec.ts), rec.chain, rec.endpoint, cu, int(rec.verified), rec.status, rec.latency_ms),
        )
        self.session_cu += cu
        self.session_calls += 1

    def total_since(self, ts: float) -> int:
        row = self.conn.execute("SELECT COALESCE(SUM(cu), 0) FROM cu_ledger WHERE ts >= ?", (int(ts),)).fetchone()
        return int(row[0])

    def today_total(self) -> int:
        day_start = int(time.time()) - (int(time.time()) % 86400)
        return self.total_since(day_start)

    def calls_since(self, ts: float) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM cu_ledger WHERE ts >= ?", (int(ts),)).fetchone()
        return int(row[0])

    def by_endpoint_since(self, ts: float) -> list[tuple[str, int, int]]:
        rows = self.conn.execute(
            "SELECT endpoint, COUNT(*), COALESCE(SUM(cu),0) FROM cu_ledger WHERE ts >= ? "
            "GROUP BY endpoint ORDER BY 3 DESC", (int(ts),)
        ).fetchall()
        return [(r[0], int(r[1]), int(r[2])) for r in rows]
