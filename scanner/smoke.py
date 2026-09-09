"""Live smoke test for the Birdeye client (T1 acceptance).

Per enabled chain:
  1. token_list_v3 (sort liquidity desc, limit 5)  -> pick a reference token, check fields
  2. ohlcv_v3 1m over the last 30 min on it          -> candle count + fields
  3. txs_token_v3 limit 20 on it                     -> trade count + fields
  4. trade_data_single                               -> expect EndpointUnavailable on 'standard'
Solana only:
  5. pre-migration check: list v3 newest listings with small mcap, then read
     `source` on a few txs to see whether pump.fun bonding-curve trades appear.

Budget: ~300 CU on the free tier.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .birdeye import BirdeyeClient, BirdeyeError, EndpointUnavailable
from .config import Config
from .db import open_db
from .ledger import CULedger
from .recorder import RawRecorder

log = logging.getLogger("smoke")

LIST_FIELDS = ["address", "symbol", "liquidity", "market_cap", "holder", "recent_listing_time",
               "last_trade_unix_time", "volume_1m_usd", "volume_5m_usd", "volume_30m_usd", "volume_1h_usd",
               "volume_5m_change_percent", "price_change_1m_percent", "price_change_5m_percent",
               "price_change_1h_percent", "trade_1m_count", "trade_5m_count", "trade_1h_count", "price"]
OHLCV_FIELDS = ["o", "h", "l", "c", "v", "v_usd", "unix_time"]
TX_FIELDS = ["tx_hash", "block_unix_time", "side", "owner", "volume_usd", "source", "price_pair"]


def _present(item: dict[str, Any], fields: list[str]) -> tuple[list[str], list[str]]:
    have = [f for f in fields if f in item]
    missing = [f for f in fields if f not in item]
    return have, missing


def _p(msg: str) -> None:
    print(msg)
    log.info(msg)


async def run_smoke(cfg: Config) -> int:
    conn = open_db(cfg.db_path)
    ledger = CULedger(conn)
    recorder = RawRecorder(cfg.raw_dir, enabled=cfg.recorder_enabled)
    start_ts = time.time()
    failures = 0
    findings: dict[str, Any] = {}

    rate_safety = float(cfg.raw.get("birdeye", {}).get("rate_safety", 0.8))
    async with BirdeyeClient(api_key=cfg.secret("BIRDEYE_API_KEY") or "", plan=cfg.birdeye_plan,
                             base_url=cfg.birdeye_base_url, recorder=recorder, ledger=ledger,
                             rate_safety=rate_safety) as be:
        _p(f"  effective rate: {be.effective_rps:.2f} rps (plan rps x rate_safety {rate_safety})")
        for ch in cfg.enabled_chains:
            chain = ch.birdeye_chain
            _p(f"\n=== {ch.name} (x-chain={chain}) ===")
            # 1. token list
            try:
                items = await be.token_list_v3(chain, sort_by="liquidity", sort_type="desc", limit=5)
            except BirdeyeError as e:
                _p(f"  [FAIL] token_list_v3: {e}")
                failures += 1
                continue
            if not items:
                _p("  [FAIL] token_list_v3 returned 0 items")
                failures += 1
                continue
            ref = items[0]
            have, missing = _present(ref, LIST_FIELDS)
            _p(f"  [OK]   token_list_v3: {len(items)} items; ref={ref.get('symbol')} {ref.get('address')}")
            _p(f"         fields present {len(have)}/{len(LIST_FIELDS)}; missing: {missing or 'none'}")
            if missing:
                _p(f"         sample keys: {sorted(ref.keys())[:40]}")
            findings[f"{ch.name}.list_missing"] = missing
            addr = ref["address"]

            # 2. ohlcv
            now = int(time.time())
            try:
                candles = await be.ohlcv_v3(chain, addr, "1m", now - 1800, now, count_limit=60)
                have, missing = _present(candles[0] if candles else {}, OHLCV_FIELDS)
                status = "OK" if candles and not missing else "WARN"
                _p(f"  [{status}]   ohlcv_v3 1m/30min: {len(candles)} candles; missing fields: {missing or 'none'}")
                if not candles:
                    failures += 1
            except BirdeyeError as e:
                _p(f"  [FAIL] ohlcv_v3: {e}")
                failures += 1

            # 3. trades v3
            try:
                txs, has_next = await be.txs_token_v3(chain, addr, limit=20)
                have, missing = _present(txs[0] if txs else {}, TX_FIELDS)
                sides = {t.get("side") for t in txs}
                status = "OK" if txs and not missing else "WARN"
                _p(f"  [{status}]   txs_token_v3: {len(txs)} trades, has_next={has_next}, sides={sides}; "
                   f"missing fields: {missing or 'none'}")
                if txs:
                    t = txs[0]
                    _p(f"         sample: side={t.get('side')} usd={t.get('volume_usd')} owner={str(t.get('owner'))[:12]}... "
                       f"source={t.get('source')} t={t.get('block_unix_time')}")
                else:
                    failures += 1
            except BirdeyeError as e:
                _p(f"  [FAIL] txs_token_v3: {e}")
                failures += 1

            # 4. gated endpoint behaviour
            try:
                td = await be.trade_data_single(chain, addr)
                keys = [k for k in td if k.startswith(("unique_wallet_5m", "volume_buy_5m", "volume_sell_5m", "buy_5m", "sell_5m"))]
                _p(f"  [OK]   trade_data_single available on plan '{cfg.birdeye_plan}'; 5m keys: {keys}")
            except EndpointUnavailable:
                _p(f"  [OK]   trade_data_single correctly gated on plan '{cfg.birdeye_plan}' (no HTTP call made)")
            except BirdeyeError as e:
                _p(f"  [WARN] trade_data_single: {e}")

            # 5. Solana pre-migration check
            if chain == "solana":
                try:
                    young = await be.token_list_v3(chain, sort_by="recent_listing_time", sort_type="desc", limit=20,
                                                   max_market_cap=60000, min_market_cap=15000, min_holder=20)
                    _p(f"  [OK]   young small-cap list: {len(young)} items")
                    pump_seen = 0
                    checked = 0
                    for it in young[:3]:
                        txs, _ = await be.txs_token_v3(chain, it["address"], limit=5)
                        checked += 1
                        srcs = {str(t.get("source")) for t in txs}
                        if any("pump" in s.lower() for s in srcs):
                            pump_seen += 1
                        _p(f"         {it.get('symbol')} mcap={it.get('market_cap')} liq={it.get('liquidity')} sources={srcs}")
                    findings["solana.pump_sources_seen"] = pump_seen
                    _p(f"  [--]   pump.fun-sourced trades seen on {pump_seen}/{checked} young tokens "
                       f"(>0 means bonding-curve tokens appear in list v3)")
                except BirdeyeError as e:
                    _p(f"  [WARN] pre-migration check: {e}")

        # summary
        _p("\n=== summary ===")
        _p(f"  calls={be.calls} retries={be.retries} limiter waits={be.limiter.waits} "
           f"(total wait {be.limiter.total_wait_s:.1f}s)")
        _p(f"  CU charged this run: {ledger.total_since(start_ts)} over {ledger.calls_since(start_ts)} ledger rows")
        for ep, n, cu in ledger.by_endpoint_since(start_ts):
            _p(f"    {ep:<22} calls={n:<3} cu={cu}")
        _p(f"  CU/rate headers seen: {sorted(be.cu_headers_seen) or 'none'}")
        _p(f"  raw records written: {recorder.records_written} -> {cfg.raw_dir}")
        if be.calls != ledger.calls_since(start_ts):
            _p(f"  [FAIL] ledger rows ({ledger.calls_since(start_ts)}) != client calls ({be.calls})")
            failures += 1
        _p(f"  findings: {findings}")
        _p("SMOKE PASSED" if failures == 0 else f"SMOKE FAILED ({failures} failure(s))")
    conn.close()
    return 0 if failures == 0 else 1
