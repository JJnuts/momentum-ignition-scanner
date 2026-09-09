"""Birdeye plan capability table and compute-unit (CU) costs.

Verified 2026-09-08 from Birdeye docs:
  plans:      price / monthly CU / rps
  gating:     endpoint availability by package
  CU costs:   token_list_v3 50 · ohlcv_v3 45/75/100 (by candle count) ·
              txs_token_v3 12 (limit<=100) · txs_token (v1) 10 (limit<=50) ·
              trade_data_single 10 · market_data_single 8 ·
              multi_price ceil(3 * n^0.8)
Anything in CU_ESTIMATE is an ESTIMATE until confirmed live.
"""
from __future__ import annotations

import math
from typing import Callable

PLAN_ORDER: list[str] = ["standard", "lite", "starter", "premium", "business"]

PLANS: dict[str, dict] = {
    "standard": {"price_usd": 0,   "rps": 1,   "monthly_cu": 30_000,     "websocket": False},
    "lite":     {"price_usd": 39,  "rps": 15,  "monthly_cu": 2_500_000,  "websocket": False},
    "starter":  {"price_usd": 99,  "rps": 15,  "monthly_cu": 8_000_000,  "websocket": False},
    "premium":  {"price_usd": 199, "rps": 50,  "monthly_cu": 20_000_000, "websocket": True},
    "business": {"price_usd": 499, "rps": 100, "monthly_cu": 60_000_000, "websocket": True},
}

# Minimum plan on which each logical endpoint is available.
ENDPOINT_MIN_PLAN: dict[str, str] = {
    "token_list_v3":          "standard",
    "token_trending":         "standard",
    "ohlcv_v3":               "standard",
    "txs_token_v3":           "standard",
    "txs_token":              "standard",
    "txs_pair":               "standard",
    "token_overview":         "standard",   # assumed; confirm in smoke
    "new_listing":            "lite",
    "trade_data_single":      "lite",
    "market_data_single":     "lite",
    "multi_price":            "lite",
    "txs_token_seek_by_time": "premium",
    "token_security":         "premium",
    "websocket":              "premium",
    "token_creation_info":    "business",
    "token_holders":          "business",
    # wallet intelligence (verified 2026-09-09; see memory / SPEC)
    "wallet_pnl_summary":     "standard",
    "token_top_traders":      "standard",
    "token_holder_profile":   "standard",   # Solana only
    "wallet_tags_tracker":    "lite",       # Solana only
    "token_first_buyers":     "lite",       # Solana only
    "smart_money_token_list": "lite",
    "wallet_identity":        "lite",       # Solana only
    "wallet_pnl_multiple":    "business",
}

# HTTP paths (base_url + path). One place to fix if Birdeye moves something.
ENDPOINT_PATH: dict[str, str] = {
    "token_list_v3":          "/defi/v3/token/list",
    "token_trending":         "/defi/token_trending",
    "ohlcv_v3":               "/defi/v3/ohlcv",
    "txs_token_v3":           "/defi/v3/token/txs",
    "txs_token":              "/defi/txs/token",
    "txs_pair":               "/defi/txs/pair",
    "txs_token_seek_by_time": "/defi/txs/token/seek_by_time",
    "token_overview":         "/defi/token_overview",
    "new_listing":            "/defi/v2/tokens/new_listing",
    "trade_data_single":      "/defi/v3/token/trade-data/single",
    "market_data_single":     "/defi/v3/token/market-data",
    "multi_price":            "/defi/multi_price",
    "token_security":         "/defi/token_security",
    "token_creation_info":    "/defi/token_creation_info",
    "token_holders":          "/defi/v3/token/holder",
    "wallet_pnl_summary":     "/wallet/v2/pnl/summary",
    "token_top_traders":      "/defi/v2/tokens/top_traders",
    "token_holder_profile":   "/token/v1/holder-profile",
    "wallet_tags_tracker":    "/token/v1/wallet-tags-tracker",
    "token_first_buyers":     "/token/v1/first-buyers",
    "smart_money_token_list": "/smart-money/v1/token/list",
    "wallet_identity":        "/identity/v1/single",
    "wallet_pnl_multiple":    "/wallet/v2/pnl/multiple",
}


def plan_rank(plan: str) -> int:
    try:
        return PLAN_ORDER.index(plan)
    except ValueError as e:
        raise ValueError(f"unknown plan {plan!r}; valid: {PLAN_ORDER}") from e


def endpoint_available(endpoint: str, plan: str) -> bool:
    """True if `endpoint` can be called on `plan`. Unknown endpoints -> False."""
    min_plan = ENDPOINT_MIN_PLAN.get(endpoint)
    if min_plan is None:
        return False
    return plan_rank(plan) >= plan_rank(min_plan)


# Verified costs. Each callable takes the relevant size argument(s).
_CU_VERIFIED: dict[str, Callable[..., int]] = {
    "token_list_v3":      lambda **kw: 50,
    "ohlcv_v3":           lambda candles=1, **kw: 45 if candles < 1000 else (75 if candles <= 2000 else 100),
    "txs_token_v3":       lambda **kw: 12,
    "txs_token":          lambda **kw: 10,
    "txs_pair":           lambda **kw: 10,
    "trade_data_single":  lambda **kw: 10,
    "market_data_single": lambda **kw: 8,
    "multi_price":        lambda n=1, **kw: int(math.ceil(3 * (max(1, n) ** 0.8))),
    "wallet_pnl_summary":     lambda **kw: 20,
    "token_top_traders":      lambda **kw: 25,
    "token_holder_profile":   lambda **kw: 25,
    "wallet_tags_tracker":    lambda **kw: 30,
    "token_first_buyers":     lambda **kw: 25,
    "smart_money_token_list": lambda **kw: 20,
    "wallet_pnl_multiple":    lambda n=1, **kw: int(math.ceil(10 * (max(1, n) ** 0.8))),
}

# Unverified estimates (flagged as such in the ledger).
CU_ESTIMATE: dict[str, int] = {
    "token_trending": 30,
    "token_overview": 30,
    "new_listing": 30,
    "token_security": 30,
    "token_creation_info": 15,
    "token_holders": 30,
    "txs_token_seek_by_time": 30,
    "wallet_identity": 10,
}


def cu_cost(endpoint: str, **size) -> tuple[int, bool]:
    """Return (cu, verified). Unknown endpoint -> (0, False)."""
    fn = _CU_VERIFIED.get(endpoint)
    if fn is not None:
        return int(fn(**size)), True
    if endpoint in CU_ESTIMATE:
        return CU_ESTIMATE[endpoint], False
    return 0, False


def daily_cu_budget(plan: str, safety_margin: float = 0.90) -> int:
    """Monthly CU spread over 30 days with a safety margin."""
    return int(PLANS[plan]["monthly_cu"] / 30 * safety_margin)
