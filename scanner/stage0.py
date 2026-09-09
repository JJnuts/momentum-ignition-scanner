"""Stage 0 - eligibility scan over Birdeye token_list v3.

Per chain, per cycle:
  build server-side filter params from config -> one list call (100 rows)
  -> normalise to TokenRow -> client-side re-verification of every filter
  -> persist a snapshot to scan_rows.

Config keys per chain (config.json -> chains.<name>.stage0):
  filters:        dict of Birdeye list-v3 params passed verbatim (min_liquidity, ...)
  alive_within_s: adds min_last_trade_unix_time = now - alive_within_s
  page_limit:     1..100
  sort_keys:      list of sort_by values, rotated one per cycle
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field, fields as dc_fields
from typing import Any, Callable

from .birdeye import BirdeyeClient, BirdeyeError
from .config import ChainConfig
from .db import meta_get, meta_set
from .ledger import CULedger
from .plans import cu_cost

log = logging.getLogger("stage0")


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def _s(v: Any) -> str | None:
    return None if v is None else str(v)


# (column, birdeye_key, caster). Column names == scan_rows columns.
FIELDS: list[tuple[str, str, Callable[[Any], Any]]] = [
    ("symbol", "symbol", _s), ("name", "name", _s),
    ("price", "price", _f), ("liquidity", "liquidity", _f), ("market_cap", "market_cap", _f), ("fdv", "fdv", _f),
    ("holder", "holder", _i),
    ("listing_ts", "recent_listing_time", _i), ("last_trade_ts", "last_trade_unix_time", _i),
    ("vol_1m", "volume_1m_usd", _f), ("vol_5m", "volume_5m_usd", _f), ("vol_30m", "volume_30m_usd", _f),
    ("vol_1h", "volume_1h_usd", _f), ("vol_24h", "volume_24h_usd", _f),
    ("vol_1m_chg", "volume_1m_change_percent", _f), ("vol_5m_chg", "volume_5m_change_percent", _f),
    ("vol_1h_chg", "volume_1h_change_percent", _f),
    ("pc_1m", "price_change_1m_percent", _f), ("pc_5m", "price_change_5m_percent", _f),
    ("pc_30m", "price_change_30m_percent", _f), ("pc_1h", "price_change_1h_percent", _f),
    ("pc_24h", "price_change_24h_percent", _f),
    ("tr_1m", "trade_1m_count", _i), ("tr_5m", "trade_5m_count", _i), ("tr_30m", "trade_30m_count", _i),
    ("tr_1h", "trade_1h_count", _i), ("tr_24h", "trade_24h_count", _i),
    ("uw_24h", "unique_wallet_24h", _i), ("buy_24h", "buy_24h", _i), ("sell_24h", "sell_24h", _i),
]
BIRDEYE_TO_COL: dict[str, str] = {bk: col for col, bk, _ in FIELDS}


@dataclass
class TokenRow:
    chain: str
    cycle_id: int
    ts: int
    address: str
    sort_key: str
    rank: int
    symbol: str | None = None
    name: str | None = None
    price: float | None = None
    liquidity: float | None = None
    market_cap: float | None = None
    fdv: float | None = None
    holder: int | None = None
    listing_ts: int | None = None
    last_trade_ts: int | None = None
    vol_1m: float | None = None
    vol_5m: float | None = None
    vol_30m: float | None = None
    vol_1h: float | None = None
    vol_24h: float | None = None
    vol_1m_chg: float | None = None
    vol_5m_chg: float | None = None
    vol_1h_chg: float | None = None
    pc_1m: float | None = None
    pc_5m: float | None = None
    pc_30m: float | None = None
    pc_1h: float | None = None
    pc_24h: float | None = None
    tr_1m: int | None = None
    tr_5m: int | None = None
    tr_30m: int | None = None
    tr_1h: int | None = None
    tr_24h: int | None = None
    uw_24h: int | None = None
    buy_24h: int | None = None
    sell_24h: int | None = None

    @classmethod
    def from_birdeye(cls, item: dict[str, Any], chain: str, cycle_id: int, ts: int,
                     sort_key: str, rank: int) -> "TokenRow":
        row = cls(chain=chain, cycle_id=cycle_id, ts=ts, address=str(item.get("address") or ""),
                  sort_key=sort_key, rank=rank)
        for col, bk, cast in FIELDS:
            setattr(row, col, cast(item.get(bk)))
        return row

    @property
    def age_s(self) -> int | None:
        return None if self.listing_ts is None else max(0, self.ts - self.listing_ts)


ROW_COLUMNS: list[str] = [f.name for f in dc_fields(TokenRow)]
_INSERT_SQL = (f"INSERT INTO scan_rows({', '.join(ROW_COLUMNS)}) "
               f"VALUES({', '.join('?' for _ in ROW_COLUMNS)})")


def build_params(stage0: dict[str, Any], now: int) -> dict[str, Any]:
    """Server-side filter params for token_list_v3."""
    params: dict[str, Any] = dict(stage0.get("filters") or {})
    alive = stage0.get("alive_within_s")
    if alive:
        params["min_last_trade_unix_time"] = int(now) - int(alive)
    return params


def verify_filters(row: TokenRow, params: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    """Client-side re-check of every min_/max_ filter. Returns violations as (param, value, limit)."""
    violations: list[tuple[str, Any, Any]] = []
    for key, limit in params.items():
        if key.startswith("min_"):
            bk, ok = key[4:], (lambda v, lim: v >= lim)
        elif key.startswith("max_"):
            bk, ok = key[4:], (lambda v, lim: v <= lim)
        else:
            continue
        col = BIRDEYE_TO_COL.get(bk)
        if col is None:
            continue  # not a field we track (e.g. min_global_fees_paid)
        val = getattr(row, col)
        if val is None or not ok(val, limit):
            violations.append((key, val, limit))
    return violations


@dataclass
class CycleResult:
    chain: str
    cycle_id: int
    ts: int
    sort_key: str
    fetched: int = 0
    persisted: int = 0
    violations: int = 0
    duplicates: int = 0
    cu: int = 0
    skipped: str | None = None      # e.g. "budget"
    error: str | None = None
    latency_ms: int = 0
    violation_samples: list[tuple[str, str, Any, Any]] = field(default_factory=list)
    rows: list["TokenRow"] = field(default_factory=list, repr=False)   # persisted rows (for Stage 1)


class Stage0Scanner:
    def __init__(self, conn: sqlite3.Connection, client: BirdeyeClient, ledger: CULedger,
                 daily_cu_cap: int, clock: Callable[[], float] = time.time) -> None:
        self.conn = conn
        self.client = client
        self.ledger = ledger
        self.daily_cu_cap = daily_cu_cap
        self._clock = clock

    # cycle ids are per chain and survive restarts (stored in meta)
    def _next_cycle_id(self, chain: str) -> int:
        key = f"cycle_id:{chain}"
        nxt = int(meta_get(self.conn, key, "0") or 0) + 1
        meta_set(self.conn, key, str(nxt))
        return nxt

    async def cycle(self, ch: ChainConfig) -> CycleResult:
        cycle_id = self._next_cycle_id(ch.name)
        sort_keys: list[str] = list(ch.stage0.get("sort_keys") or ["liquidity"])
        sort_key = sort_keys[(cycle_id - 1) % len(sort_keys)]
        now = int(self._clock())
        res = CycleResult(chain=ch.name, cycle_id=cycle_id, ts=now, sort_key=sort_key)
        params = build_params(ch.stage0, now)
        limit = int(ch.stage0.get("page_limit", 100))

        cu, _ = cu_cost("token_list_v3")
        if self.ledger.today_total() + cu > self.daily_cu_cap:
            res.skipped = "budget"
            log.warning("%s cycle %d skipped: daily CU cap %d would be exceeded (today=%d)",
                        ch.name, cycle_id, self.daily_cu_cap, self.ledger.today_total())
            return res

        t0 = time.monotonic()
        try:
            items = await self.client.token_list_v3(ch.birdeye_chain, sort_by=sort_key, sort_type="desc",
                                                    limit=limit, **params)
        except BirdeyeError as e:
            res.error = str(e)
            res.latency_ms = int((time.monotonic() - t0) * 1000)
            log.error("%s cycle %d failed: %s", ch.name, cycle_id, e)
            return res
        res.latency_ms = int((time.monotonic() - t0) * 1000)
        res.cu = cu
        res.fetched = len(items)

        rows: list[TokenRow] = []
        seen: set[str] = set()
        for rank, item in enumerate(items):
            row = TokenRow.from_birdeye(item, ch.name, cycle_id, now, sort_key, rank)
            if not row.address:
                continue
            if row.address in seen:
                res.duplicates += 1
                continue
            bad = verify_filters(row, params)
            if bad:
                res.violations += 1
                if len(res.violation_samples) < 5:
                    res.violation_samples.append((row.address, *bad[0]))
                continue
            seen.add(row.address)
            rows.append(row)
        self._persist(rows)
        res.persisted = len(rows)
        res.rows = rows
        return res

    def _persist(self, rows: list[TokenRow]) -> None:
        if not rows:
            return
        self.conn.execute("BEGIN")
        try:
            self.conn.executemany(_INSERT_SQL, [tuple(getattr(r, c) for c in ROW_COLUMNS) for r in rows])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
