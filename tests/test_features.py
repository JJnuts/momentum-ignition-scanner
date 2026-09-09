import json
from pathlib import Path

import pytest

from scanner.features import Bar, compute, find_anchor, last_n_window, make_bars, window_stats
from scanner.tape import Trade

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
FCFG = CFG.get("stage2", {}).get("features", {})
T0 = 1_788_866_000


def tr(i, ts, side="buy", usd=10.0, wallet=None, price=1.0, amount=None):
    amount = amount if amount is not None else usd / price
    return Trade(chain="solana", address="TOK", sig=f"s{i}", ts=ts, side=side, wallet=wallet or f"w{i}",
                 usd=usd, price=price, amount=amount, source="x", tx_hash=f"s{i}")


def quiet(n, start=T0, step=10, usd=10.0, price=1.0):
    """1 trade every `step` s, alternating buy/sell, distinct wallets, flat price."""
    return [tr(i, start + i * step, "buy" if i % 2 == 0 else "sell", usd, price=price) for i in range(n)]


# ---- windows ----------------------------------------------------------------------

def test_ofi_extremes_and_alternating():
    allbuy = [tr(i, T0 + i, "buy", 10) for i in range(30)]
    allsell = [tr(i, T0 + i, "sell", 10) for i in range(30)]
    alt = [tr(i, T0 + i, "buy" if i % 2 == 0 else "sell", 10) for i in range(30)]
    assert compute(allbuy, FCFG).ofi30.ofi == pytest.approx(1.0)
    assert compute(allsell, FCFG).ofi30.ofi == pytest.approx(-1.0)
    assert compute(alt, FCFG).ofi30.ofi == pytest.approx(0.0)
    assert compute(alt, FCFG).ofi30.buy_share == pytest.approx(0.5)


def test_ofi_is_usd_weighted_not_count_weighted():
    # 9 tiny buys, 1 big sell -> negative OFI although buys outnumber sells 9:1
    t = [tr(i, T0 + i, "buy", 1.0) for i in range(9)] + [tr(9, T0 + 9, "sell", 91.0)]
    f = compute(t, {**FCFG, "min_window_trades": 5})
    assert f.recent.ofi == pytest.approx((9 - 91) / 100)
    assert f.recent.buyers == 9 and f.recent.sellers == 1 and f.recent.buyer_seller_ratio == 9.0


def test_window_respects_count_max_age_and_min_n():
    t = quiet(40)                                   # spans 390 s
    a, b = last_n_window(t, 30, max_age_s=100, as_of=t[-1].ts)   # only trades within 100 s -> 11 trades
    assert b == 40 and a == 29
    f = compute(t, {**FCFG, "ofi_window_trades": 30, "window_max_age_s": 100, "min_window_trades": 12})
    assert f.ofi30.n == 11 and f.ofi30.ofi is None          # below min_n -> undefined
    f2 = compute(t, {**FCFG, "ofi_window_trades": 30, "window_max_age_s": 100, "min_window_trades": 8})
    assert f2.ofi30.n == 11 and f2.ofi30.ofi is not None


def test_new_wallet_share_uses_wallets_seen_before_the_window():
    old = [tr(i, T0 + i, "buy", 10, wallet="A") for i in range(20)]           # wallet A everywhere
    win_old = [tr(100 + i, T0 + 100 + i, "buy", 10, wallet="A") for i in range(10)]
    win_new = [tr(200 + i, T0 + 200 + i, "buy", 10, wallet=f"N{i}") for i in range(10)]
    cfg = {**FCFG, "recent_window_trades": 10, "min_window_trades": 5}
    assert compute(old + win_old, cfg).recent.new_wallet_share_usd == pytest.approx(0.0)
    assert compute(old + win_new, cfg).recent.new_wallet_share_usd == pytest.approx(1.0)
    assert compute(old + win_new, cfg).recent.new_wallets == 10


def test_window_stats_direct():
    t = [tr(0, T0, "buy", 40, "A"), tr(1, T0 + 5, "sell", 10, "B"), tr(2, T0 + 9, "buy", 50, "A")]
    st = window_stats(t, 0, 3, prior_wallets=set(), min_n=2)
    assert st.n == 3 and st.span_s == 9 and st.buy_usd == 90 and st.sell_usd == 10
    assert st.ofi == pytest.approx(0.8) and st.buyers == 1 and st.sellers == 1
    assert st.trades_per_wallet == pytest.approx(1.5) and st.usd_rate == pytest.approx(100 / 9)


# ---- anchor -----------------------------------------------------------------------

def burst_tape(quiet_n=180, burst_n=40, burst_step=0.5):
    q = quiet(quiet_n)                                    # 30 min, $10 per 10 s = $1/s
    t_burst = q[-1].ts + 10
    b = [tr(1000 + i, int(t_burst + i * burst_step), "buy", 30.0, wallet=f"B{i}", price=1.0 + 0.01 * i)
         for i in range(burst_n)]                         # $30 per 0.5 s = $60/s, distinct buyers
    return q + b, len(q)


def test_anchor_found_inside_burst_and_absent_on_quiet_prefix():
    t, qn = burst_tape()
    f = compute(t, FCFG)
    assert f.anchor_idx is not None and qn <= f.anchor_idx < qn + 40
    assert f.anchor_rate_ratio >= FCFG.get("anchor_rate_mult", 4.0)
    assert f.trades_since_anchor == len(t) - 1 - f.anchor_idx
    assert f.seconds_since_anchor == t[-1].ts - t[f.anchor_idx].ts
    # the quiet prefix alone has no anchor
    assert compute(t[:qn], FCFG).anchor_idx is None
    # anchor low / price come from the onset window, price_vs_anchor positive on a rising burst
    assert f.anchor_low is not None and f.price_vs_anchor_pct > 0


def test_anchor_requires_distinct_buyers():
    q = quiet(180)
    t_burst = q[-1].ts + 10
    one_wallet = [tr(1000 + i, int(t_burst + i * 0.5), "buy", 30.0, wallet="SAME") for i in range(40)]
    f = compute(q + one_wallet, FCFG)
    assert f.anchor_idx is None                     # volume spike from one wallet is not ignition


def test_anchor_needs_trailing_history():
    # burst only, no trailing baseline -> undefined
    b = [tr(i, T0 + i, "buy", 30.0, wallet=f"B{i}") for i in range(60)]
    assert compute(b, FCFG).anchor_idx is None


def test_anchor_picks_earliest_onset_within_lookback():
    t, qn = burst_tape(burst_n=60)
    f_all = compute(t, FCFG)
    # evaluating a little later (more burst trades) must keep the SAME onset
    f_prefix = compute(t[:qn + 45], FCFG)
    assert f_prefix.anchor_idx == f_all.anchor_idx


# ---- structure --------------------------------------------------------------------

def test_bars_clv_higher_lows_and_rejection():
    # 3 bars of 10 trades: lows rising, closes near highs; then a huge-volume bar with a long upper wick
    prices = ([1.00, 1.05, 0.98, 1.06, 1.02, 1.08, 1.04, 1.09, 1.07, 1.10] +      # low .98 close 1.10
              [1.10, 1.12, 1.05, 1.15, 1.11, 1.16, 1.13, 1.18, 1.15, 1.19] +      # low 1.05 close 1.19
              [1.19, 1.22, 1.12, 1.25, 1.20, 1.27, 1.23, 1.28, 1.26, 1.29])       # low 1.12 close 1.29
    t = [tr(i, T0 + i, "buy", 10.0, price=p) for i, p in enumerate(prices)]
    bars = make_bars(t, 0, 10, 30)
    assert len(bars) == 3
    assert [round(b.l, 2) for b in bars] == [0.98, 1.05, 1.12] and bars[-1].c == 1.29
    f = compute(t, {**FCFG, "anchor_lookback_s": 0})   # no anchor: bars over the tail
    assert f.bars_n == 3 and f.higher_lows_2of3 is True and f.clv_ge_half_2of3 is True
    assert f.clv_last == pytest.approx((1.29 - 1.12) / (1.29 - 1.12))
    # rejection: add a bar with 10x volume that spikes to 1.60 and closes at 1.30
    spike = [1.30, 1.45, 1.60, 1.55, 1.40, 1.35, 1.32, 1.31, 1.30, 1.30]
    t2 = t + [tr(100 + i, T0 + 100 + i, "buy", 100.0, price=p) for i, p in enumerate(spike)]
    f2 = compute(t2, {**FCFG, "anchor_lookback_s": 0})
    assert f2.rejection_wick_frac == pytest.approx((1.60 - 1.30) / (1.60 - 1.30))   # o=1.30,c=1.30,h=1.60,l=1.30


def test_avwap_is_exact_usd_over_amount():
    t, qn = burst_tape()
    f = compute(t, FCFG)
    since = t[f.anchor_idx - FCFG.get("ignition_window_trades", 20) + 1:]
    assert f.avwap == pytest.approx(sum(x.usd for x in since) / sum(x.amount for x in since))
    assert f.price_vs_avwap_pct is not None


def test_partial_bar_rule():
    t = [tr(i, T0 + i, "buy", 10, price=1.0) for i in range(13)]
    assert len(make_bars(t, 0, 10, 30)) == 1            # 3 leftover trades < half a bar -> dropped
    t = [tr(i, T0 + i, "buy", 10, price=1.0) for i in range(16)]
    assert len(make_bars(t, 0, 10, 30)) == 2            # 6 leftover >= 5 -> kept


# ---- invariants ---------------------------------------------------------------------

def test_no_lookahead_prefix_equals_as_of():
    """Adding future trades must not change features computed as of an earlier time.
    Several trades can share one second, so the cut is taken at a timestamp boundary."""
    t, qn = burst_tape(burst_n=60)
    checked = 0
    for k in (150, qn + 5, qn + 25, qn + 50, len(t)):
        while k < len(t) and t[k - 1].ts == t[k].ts:   # move to the end of that second
            k += 1
        a = compute(t[:k], FCFG).to_dict()
        b = compute(t, FCFG, as_of=t[k - 1].ts).to_dict()
        assert a == b, k
        checked += 1
    assert checked == 5


def test_same_second_trades_are_all_included_as_of_that_second():
    t = [tr(i, T0 + (i // 3), "buy", 10.0) for i in range(30)]     # 3 trades per second
    f = compute(t, {**FCFG, "min_window_trades": 2}, as_of=T0 + 4)
    assert f.n == 15 and f.newest_ts == T0 + 4


def test_empty_and_unpriced_tapes_are_safe():
    assert compute([], FCFG).n == 0
    t = [Trade("solana", "TOK", f"s{i}", T0 + i, "buy", f"w{i}", 5.0, None, None, "x") for i in range(30)]
    f = compute(t, FCFG)
    assert f.price is None and f.bars_n == 0 and f.ofi30.ofi == pytest.approx(1.0)
