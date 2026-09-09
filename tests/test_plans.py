import pytest

from scanner.plans import (ENDPOINT_MIN_PLAN, ENDPOINT_PATH, PLANS, cu_cost, daily_cu_budget,
                           endpoint_available, plan_rank)


def test_plan_order():
    assert plan_rank("standard") < plan_rank("lite") < plan_rank("starter") < plan_rank("premium") < plan_rank("business")
    with pytest.raises(ValueError):
        plan_rank("platinum")


def test_gating_matches_verified_table():
    # free tier
    for ep in ("token_list_v3", "ohlcv_v3", "txs_token_v3", "txs_token"):
        assert endpoint_available(ep, "standard"), ep
    for ep in ("new_listing", "trade_data_single", "multi_price", "token_security"):
        assert not endpoint_available(ep, "standard"), ep
    # starter
    assert endpoint_available("trade_data_single", "starter")
    assert not endpoint_available("token_security", "starter")
    assert not endpoint_available("websocket", "starter")
    # premium / business
    assert endpoint_available("token_security", "premium")
    assert not endpoint_available("token_creation_info", "premium")
    assert endpoint_available("token_creation_info", "business")
    # unknown endpoint is never available
    assert not endpoint_available("made_up", "business")


def test_every_gated_endpoint_has_a_path_except_websocket():
    for ep in ENDPOINT_MIN_PLAN:
        if ep == "websocket":
            continue
        assert ep in ENDPOINT_PATH, ep
        assert ENDPOINT_PATH[ep].startswith("/"), ep


def test_verified_cu_costs():
    assert cu_cost("token_list_v3") == (50, True)
    assert cu_cost("ohlcv_v3", candles=100) == (45, True)
    assert cu_cost("ohlcv_v3", candles=1500) == (75, True)
    assert cu_cost("ohlcv_v3", candles=3000) == (100, True)
    assert cu_cost("txs_token_v3") == (12, True)
    assert cu_cost("txs_token") == (10, True)
    assert cu_cost("trade_data_single") == (10, True)
    assert cu_cost("market_data_single") == (8, True)
    assert cu_cost("multi_price", n=1) == (3, True)
    assert cu_cost("multi_price", n=100) == (120, True)


def test_estimated_and_unknown_costs_are_flagged():
    cu, verified = cu_cost("token_security")
    assert cu > 0 and verified is False
    assert cu_cost("made_up") == (0, False)


def test_daily_budget():
    assert daily_cu_budget("standard") == int(30_000 / 30 * 0.9)
    assert daily_cu_budget("starter") == int(PLANS["starter"]["monthly_cu"] / 30 * 0.9)
