import json
import random
from pathlib import Path

import pytest

from scanner.features import compute
from scanner.tape import Trade
from scanner.wash import (distribution_check, evaluate, holdings_from_top_traders, parse_tag_flows,
                          tag_vetoes, wash_components, _settings)

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
FCFG = CFG.get("stage2", {}).get("features", {})
WCFG = CFG.get("stage2", {}).get("wash", {})
T0 = 1_788_866_000


def tr(i, ts, side="buy", usd=10.0, wallet=None, price=1.0):
    return Trade("solana", "TOK", f"s{i}", ts, side, wallet or f"w{i}", usd, price, usd / price, "x", f"s{i}")


def organic_tape(n=80, seed=1):
    """Distinct wallets, lognormal sizes, buy-heavy, gently rising price."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        side = "buy" if rng.random() < 0.65 else "sell"
        usd = rng.lognormvariate(3.5, 1.0)
        out.append(tr(i, T0 + i * 3, side, usd, wallet=f"h{i}", price=1.0 + 0.002 * i))
    return out


def wash_tape(n=80):
    """Five wallets round-tripping uniform $20 clips, buy/sell alternating, flat price."""
    return [tr(i, T0 + i * 2, "buy" if i % 2 == 0 else "sell", 20.0, wallet=f"bot{i % 5}", price=1.0) for i in range(n)]


def test_wash_tape_scores_high_and_is_vetoed():
    t = wash_tape()
    f = compute(t, FCFG)
    r = evaluate(t, f, WCFG, liquidity=500.0)
    assert r.roundtrip_share == pytest.approx(1.0)          # every wallet both bought and sold
    assert r.top3_share >= 0.6 and r.size_uniformity == pytest.approx(1.0)
    assert r.count_trap is True and r.trades_per_wallet == pytest.approx(60 / 5)
    assert r.wash_score is not None and r.wash_score >= 0.6
    assert "WASH" in r.hard_vetoes


def test_organic_tape_scores_low():
    t = organic_tape()
    f = compute(t, FCFG)
    r = evaluate(t, f, WCFG, liquidity=50_000.0)
    assert r.roundtrip_share == pytest.approx(0.0) and r.trades_per_wallet == pytest.approx(1.0)
    assert r.size_uniformity < 0.75
    assert r.wash_score is not None and r.wash_score <= 0.3, r.components
    assert r.hard_vetoes == []


def test_wash_score_undefined_below_min_trades():
    t = wash_tape(8)
    r = evaluate(t, compute(t, FCFG), {**WCFG, "min_trades": 12})
    assert r.wash_score is None and r.vetoes == []


def test_distribution_veto_needs_concentration_and_supply():
    # many small organic buys, sells concentrated in two wallets -> OFI still positive
    buys = [tr(i, T0 + i, "buy", 15.0, wallet=f"b{i}") for i in range(40)]
    dump = [tr(100 + i, T0 + 50 + i, "sell", 120.0, wallet="DEV" if i % 2 == 0 else "DEV2") for i in range(6)]
    t = buys + dump
    f = compute(t, FCFG)
    assert f.wash.ofi is not None and f.wash.ofi > -1.0
    # holdings unknown -> only a soft suspect flag (share is 100%)
    r0 = evaluate(t, f, WCFG, liquidity=50_000.0)
    assert r0.top3_seller_share == pytest.approx(1.0)
    assert "DISTRIBUTION_SUSPECT" in r0.soft_flags and "DISTRIBUTION" not in r0.hard_vetoes
    # holdings known and the seller holds 5% of supply -> hard veto
    r1 = evaluate(t, f, WCFG, liquidity=50_000.0, holdings_pct={"DEV": 5.0, "DEV2": 0.4})
    assert "DISTRIBUTION" in r1.hard_vetoes and r1.top_seller_supply_pct == 5.0
    # holdings known but small holders -> no veto
    r2 = evaluate(t, f, WCFG, liquidity=50_000.0, holdings_pct={"DEV": 0.5, "DEV2": 0.4})
    assert r2.hard_vetoes == [] and "DISTRIBUTION_SUSPECT" not in r2.soft_flags


def test_distribution_check_no_sells():
    v, share, pct = distribution_check([tr(0, T0, "buy", 10)], None, _settings(WCFG))
    assert v is None and share is None


def test_rejection_veto_from_features():
    base = [tr(i, T0 + i, "buy", 10.0, price=1.0 + 0.001 * i) for i in range(30)]
    spike = [1.05, 1.30, 1.50, 1.45, 1.20, 1.10, 1.06, 1.05, 1.05, 1.05]     # o=1.05 c=1.05 h=1.50 l=1.05 -> wick 1.0
    t = base + [tr(100 + i, T0 + 100 + i, "buy", 200.0, price=p) for i, p in enumerate(spike)]
    f = compute(t, {**FCFG, "anchor_lookback_s": 0})
    r = evaluate(t, f, WCFG)
    assert "REJECTION" in r.hard_vetoes


def test_tag_flow_parsing_and_vetoes():
    payload = {"groups": {
        "dev": [{"volume_sell_usd": 900, "volume_buy_usd": 0, "wallet_sell_count": 1},
                {"volume_sell_usd": 100, "volume_buy_usd": 0, "wallet_sell_count": 1}],
        "smart_trader": {"volume_buy_usd": 5000, "volume_sell_usd": 200, "wallet_buy_count": 4},
        "bundler": {"items": [{"volume_sell_usd": 3000}]},
        "kol": "garbage",
    }}
    flows = parse_tag_flows(payload)
    assert flows["dev"]["sell_usd"] == 1000 and flows["smart_trader"]["buy_usd"] == 5000
    assert flows["bundler"]["sell_usd"] == 3000 and flows["kol"]["sell_usd"] == 0
    s = _settings(WCFG)
    v = tag_vetoes(flows, window_sell_usd=4000, s=s)
    names = {x.name for x in v}
    assert "DEV_INSIDER_SELLING" in names and "BUNDLER_SELLING" in names     # 25% and 75% of sells
    assert tag_vetoes(flows, window_sell_usd=100_000, s=s) == []              # negligible vs window
    assert tag_vetoes({}, 1000, s) == [] and parse_tag_flows({"groups": []}) == {}


def test_holdings_from_top_traders():
    items = [{"owner": "A", "holdVolume": 5_000_000}, {"owner": "B", "holdVolume": "x"}, {"holdVolume": 1}]
    assert holdings_from_top_traders(items, supply=100_000_000) == {"A": 5.0}
    assert holdings_from_top_traders(items, supply=None) == {}


def test_evaluate_respects_as_of_and_window():
    t = organic_tape(100)
    f = compute(t, FCFG)
    r_all = evaluate(t, f, {**WCFG, "window_trades": 60})
    r_cut = evaluate(t, f, {**WCFG, "window_trades": 60}, as_of=t[49].ts)
    assert r_all.n == 60 and r_cut.n == 50
    d = r_all.to_dict()
    assert "hard_vetoes" in d and "components" in d
