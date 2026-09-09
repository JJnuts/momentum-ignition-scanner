"""Per-candidate Birdeye enrichments for the vetoes (T7), cached in SQLite.

  holdings   token_top_traders (25 CU): per-wallet supply % for the DISTRIBUTION veto.
             Needs the token supply = market_cap / price from the latest scan row.
  tag_flows  wallet-tags-tracker (30 CU, Solana only): dev/sniper/smart_trader/kol
             buy-sell USD over a lookback, for the DEV/INSIDER and BUNDLER vetoes.

Both are fetched at most once per cache window per candidate and budgeted.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Callable

from .birdeye import BirdeyeClient, BirdeyeError, EndpointUnavailable
from .config import ChainConfig
from .ledger import CULedger
from .plans import cu_cost
from .wash import holdings_from_top_traders, parse_tag_flows

log = logging.getLogger("enrich")


class Enricher:
    def __init__(self, conn: sqlite3.Connection, client: BirdeyeClient | None, ledger: CULedger | None,
                 settings: dict[str, Any], daily_cu_cap: int, clock: Callable[[], float] = time.time) -> None:
        self.conn = conn
        self.client = client
        self.ledger = ledger
        self.daily_cu_cap = daily_cu_cap
        self._clock = clock
        self.holdings_enabled = bool(settings.get("holdings_enabled", True))
        self.holdings_cache_s = int(settings.get("holdings_cache_s", 300))
        self.tag_flows_enabled = bool(settings.get("tag_flows_enabled", True))
        self.tag_flows_cache_s = int(settings.get("tag_flows_cache_s", 300))
        self.tag_flows_lookback_s = int(settings.get("tag_flows_lookback_s", 1800))
        self.daily_cu_budget = int(settings.get("daily_cu_budget", 40_000))
        self.cu_today = 0
        self._day = int(clock() // 86400)
        self.calls = 0
        self.cache_hits = 0
        self.budget_skips = 0

    def _budget_ok(self, cu: int) -> bool:
        d = int(self._clock() // 86400)
        if d != self._day:
            self._day, self.cu_today = d, 0
        if self.cu_today + cu > self.daily_cu_budget:
            return False
        if self.ledger is not None and self.ledger.today_total() + cu > self.daily_cu_cap:
            return False
        return True

    def _cached(self, chain: str, address: str, kind: str, max_age_s: int) -> Any | None:
        r = self.conn.execute("SELECT fetched_ts, payload FROM enrichment WHERE chain=? AND address=? AND kind=?",
                              (chain, address, kind)).fetchone()
        if r is None or self._clock() - int(r["fetched_ts"]) > max_age_s:
            return None
        self.cache_hits += 1
        return json.loads(r["payload"])

    def _store(self, chain: str, address: str, kind: str, payload: Any) -> None:
        self.conn.execute(
            "INSERT INTO enrichment(chain, address, kind, fetched_ts, payload) VALUES(?,?,?,?,?) "
            "ON CONFLICT(chain, address, kind) DO UPDATE SET fetched_ts=excluded.fetched_ts, payload=excluded.payload",
            (chain, address, kind, int(self._clock()), json.dumps(payload, separators=(",", ":"), default=str)))

    def _supply(self, chain: str, address: str) -> float | None:
        r = self.conn.execute("SELECT market_cap, price FROM scan_rows WHERE chain=? AND address=? AND price>0 "
                              "ORDER BY ts DESC LIMIT 1", (chain, address)).fetchone()
        if r is None or not r["market_cap"] or not r["price"]:
            return None
        return float(r["market_cap"]) / float(r["price"])

    async def holdings(self, ch: ChainConfig, address: str) -> dict[str, float] | None:
        """{wallet: supply %} for the token's top traders, or None if unavailable."""
        if not self.holdings_enabled or self.client is None:
            return None
        cached = self._cached(ch.name, address, "holdings", self.holdings_cache_s)
        if cached is not None:
            return {k: float(v) for k, v in cached.items()}
        cu, _ = cu_cost("token_top_traders")
        if not self._budget_ok(cu):
            self.budget_skips += 1
            return None
        try:
            items = await self.client.token_top_traders(ch.birdeye_chain, address, time_frame="1h",
                                                        sort_by="volume", limit=10)
        except (EndpointUnavailable, BirdeyeError) as e:
            log.warning("holdings fetch failed for %s %s: %s", ch.name, address[:8], e)
            return None
        self.calls += 1
        self.cu_today += cu
        out = holdings_from_top_traders(items, self._supply(ch.name, address))
        self._store(ch.name, address, "holdings", out)
        return out

    async def tag_flows(self, ch: ChainConfig, address: str, now: int | None = None) -> dict[str, dict[str, float]] | None:
        if not self.tag_flows_enabled or self.client is None or ch.birdeye_chain != "solana":
            return None
        cached = self._cached(ch.name, address, "tag_flows", self.tag_flows_cache_s)
        if cached is not None:
            return cached
        cu, _ = cu_cost("wallet_tags_tracker")
        if not self._budget_ok(cu):
            self.budget_skips += 1
            return None
        now = int(self._clock()) if now is None else now
        try:
            payload = await self.client.wallet_tags_tracker(ch.birdeye_chain, address, time_from=now - self.tag_flows_lookback_s,
                                                            time_to=now, time_frame="5m",
                                                            tags=["dev", "sniper", "smart_trader", "kol"],
                                                            top_10_holder=True)
        except (EndpointUnavailable, BirdeyeError) as e:
            log.warning("tag flows fetch failed for %s %s: %s", ch.name, address[:8], e)
            return None
        self.calls += 1
        self.cu_today += cu
        flows = parse_tag_flows(payload)
        self._store(ch.name, address, "tag_flows", flows)
        return flows
