import json
from pathlib import Path

import pytest

from scanner.config import ChainConfig
from scanner.db import open_db
from scanner.stage0 import ROW_COLUMNS, TokenRow
from scanner.stage1 import (RVOL_CAP, Stage1, age_bucket, base_rate, cohort_z, mcap_bucket, pct_rank, rvol)

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
NOW = 1_788_866_000


SOL_TEST_OVERRIDES = dict(rvol_1m_min=4.0, rvol_5m_min=3.0, percentile_gates={"rvol_1m_top_pct": 5, "trade_5m_count_top_pct": 10},
                          impact_eff_min_percentile=40)


def sol_cfg(**over) -> ChainConfig:
    s1 = dict(CFG["chains"]["solana"]["stage1"])
    s1.update(SOL_TEST_OVERRIDES)
    s1.update(over)
    return ChainConfig("solana", True, "solana", 60, CFG["chains"]["solana"]["stage0"], s1)


RH_TEST_OVERRIDES = dict(percentile_gates={}, vol_1h_min_usd=0, rvol_dt_min=3.0, d_trades_min=5,
                         fallback_vol_1h_chg_min_pct=200.0, fallback_trade_1h_count_min=20,
                         price_change_1h_min_pct=4.0)


def rh_cfg(**over) -> ChainConfig:
    s1 = dict(CFG["chains"]["robinhood"]["stage1"])
    s1.update(RH_TEST_OVERRIDES)
    s1.update(over)
    return ChainConfig("robinhood", True, "robinhood", 120, CFG["chains"]["robinhood"]["stage0"], s1)


def sol_row(address="A", ts=NOW, cycle_id=1, **over) -> TokenRow:
    """A quiet, mature Solana token (passes nothing by itself)."""
    base = dict(chain="solana", cycle_id=cycle_id, ts=ts, address=address, sort_key="x", rank=0, symbol=address,
                price=1.0, liquidity=50_000.0, market_cap=200_000.0, holder=500, listing_ts=ts - 86400,
                last_trade_ts=ts, vol_1m=100.0, vol_5m=500.0, vol_30m=3_000.0, vol_1h=6_000.0,
                pc_1m=0.1, pc_5m=0.5, pc_30m=1.0, pc_1h=2.0, tr_1m=2, tr_5m=10, tr_30m=60, tr_1h=120)
    base.update(over)
    return TokenRow(**base)


def ignite(address="IGN", **over) -> TokenRow:
    """A textbook ignition: 1m and 5m volume far above the trailing baseline, price up, participation up."""
    d = dict(vol_1m=2_000.0, vol_5m=6_000.0, vol_30m=9_000.0, vol_1h=12_000.0,
             pc_1m=3.0, pc_5m=15.0, pc_1h=25.0, tr_1m=20, tr_5m=60)
    d.update(over)
    return sol_row(address=address, **d)


def rh_row(address="0xR", ts=NOW, cycle_id=1, **over) -> TokenRow:
    base = dict(chain="robinhood", cycle_id=cycle_id, ts=ts, address=address, sort_key="x", rank=0, symbol=address,
                price=1.0, liquidity=20_000.0, market_cap=100_000.0, holder=None, listing_ts=ts - 86400,
                last_trade_ts=ts, vol_1h=3_600.0, vol_1h_chg=10.0, pc_1h=1.0, tr_1h=60, vol_24h=50_000.0)
    base.update(over)
    return TokenRow(**base)


def insert_rows(conn, rows):
    conn.executemany(f"INSERT INTO scan_rows({', '.join(ROW_COLUMNS)}) VALUES({', '.join('?' * len(ROW_COLUMNS))})",
                     [tuple(getattr(r, c) for c in ROW_COLUMNS) for r in rows])


# ---- primitives -----------------------------------------------------------------

def test_base_rate_excludes_current_window_and_shrinks_with_age():
    # mature: 6000 in 1h incl. 500 in last 5m -> 5500 over 11 prior 5m units = 500/5m
    assert base_rate(6000, 500, 3600, 300, None) == pytest.approx(500.0)
    # 20-minute-old token: window shrinks to 1200s -> 3 prior units
    assert base_rate(6000, 500, 3600, 300, 1200) == pytest.approx(5500 / 3)
    # under 2 units of history -> undefined
    assert base_rate(6000, 500, 3600, 300, 500) is None
    assert base_rate(None, 500, 3600, 300, None) is None


def test_rvol_cap_and_zero_baseline():
    assert rvol(1000, 100) == pytest.approx(10.0)
    assert rvol(1000, 0) == RVOL_CAP
    assert rvol(0, 0) is None
    assert rvol(10_000_000, 1) == RVOL_CAP


def test_pct_rank_and_buckets():
    assert pct_rank([1, 2, 3, 4, 5], 5) == 100.0
    assert pct_rank([1, 2, 3, 4, 5], 1) == 0.0
    assert pct_rank([1, 2, 3, 4, 5], 3) == 50.0
    assert pct_rank([1], 1) is None
    assert age_bucket(None) == "unknown" and age_bucket(100) == "lt15m" and age_bucket(10_000) == "2h-24h"
    assert mcap_bucket(50_000) == "20k-100k" and mcap_bucket(5e6) == "gt1M"


def test_cohort_z_falls_back_when_cohort_small():
    z, n = cohort_z(5.0, cohort=[1.0, 2.0], fallback=[1.0, 2.0, 3.0, 4.0], min_n=3)
    assert n == 4 and z > 0
    z2, n2 = cohort_z(5.0, cohort=[1.0, 2.0, 3.0], fallback=[], min_n=3)
    assert n2 == 3 and z2 > 0
    assert cohort_z(None, [1, 2, 3], [], 3) == (None, 0)
    assert cohort_z(2.0, [2.0, 2.0, 2.0], [], 3) == (0.0, 3)


# ---- short mode ----------------------------------------------------------------------

def test_ignition_row_passes_and_quiet_rows_fail(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    s1 = Stage1(conn, persist=True)
    # a page: one igniter plus quiet rows so percentiles are meaningful
    rows = [ignite()] + [sol_row(address=f"Q{i}") for i in range(20)]
    st = s1.run_cycle(sol_cfg(), rows, NOW, cycle_id=1)
    assert st.evaluated == 21 and st.passed == 1 and st.nominated == 1
    assert st.nominees[0][1] == "IGN"
    nom = conn.execute("SELECT chain, address, tier, features_json, gates_json FROM nominations").fetchone()
    assert nom["tier"] == "WATCH" and nom["address"] == "IGN"
    feats = json.loads(nom["features_json"])
    assert feats["mode"] == "short" and feats["rvol_1m"] > 4 and feats["rvol_5m"] > 3
    gates = json.loads(nom["gates_json"])
    assert all(g["pass"] for g in gates.values() if g["kind"] != "soft")
    # quiet rows fail on the ignition gates first
    assert st.fail_counts.most_common(1)[0][0] in ("rvol_1m", "rvol_5m")


def test_cooldown_blocks_renomination_and_survives_restart(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    rows = [ignite()] + [sol_row(address=f"Q{i}") for i in range(20)]
    s1 = Stage1(conn, persist=True)
    assert s1.run_cycle(sol_cfg(), rows, NOW, 1).nominated == 1
    st2 = s1.run_cycle(sol_cfg(), rows, NOW + 60, 2)
    assert st2.passed == 1 and st2.nominated == 0 and st2.cooldown == 1
    # new instance loads cooldown state from DB
    s1b = Stage1(conn, persist=True, clock=lambda: NOW + 120)
    assert s1b.run_cycle(sol_cfg(), rows, NOW + 120, 3).cooldown == 1
    # after the cooldown window it is nominated again
    assert s1b.run_cycle(sol_cfg(), rows, NOW + 1300, 4).nominated == 1
    assert conn.execute("SELECT COUNT(*) FROM nominations").fetchone()[0] == 2


@pytest.mark.parametrize("over, gate", [
    (dict(pc_5m=80.0), "price_change_5m_max"),          # extended
    (dict(pc_1h=200.0), "price_change_1h_max"),         # 4th leg
    (dict(pc_1m=-12.0), "price_change_1m_min"),         # mid-dump
    (dict(pc_5m=2.0), "price_change_5m_min"),           # no price response
    (dict(tr_1m=3), "trade_1m_count"),                  # thin participation
    (dict(listing_ts=NOW - 100), "min_age"),            # too young
])
def test_each_veto_blocks_an_otherwise_good_ignition(tmp_path, over, gate):
    conn = open_db(tmp_path / "t.sqlite")
    s1 = Stage1(conn, persist=False)
    rows = [ignite(**over)] + [sol_row(address=f"Q{i}") for i in range(20)]
    ev = {e.row.address: e for e in s1.evaluate(sol_cfg(), rows, NOW)}["IGN"]
    assert not ev.passed and gate in ev.fail_reasons


def test_wash_turnover_veto(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    s1 = Stage1(conn, persist=False)
    # huge turnover (vol_5m = 3x liquidity) with flat price = self-trading
    wash = ignite(vol_5m=150_000.0, vol_1h=160_000.0, vol_1m=30_000.0, vol_30m=40_000.0, pc_5m=1.0, pc_1m=0.2)
    rows = [wash] + [sol_row(address=f"Q{i}") for i in range(20)]
    ev = {e.row.address: e for e in s1.evaluate(sol_cfg(), rows, NOW)}["IGN"]
    assert "wash_turnover" in ev.fail_reasons


def test_negative_impact_efficiency_veto(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    s1 = Stage1(conn, persist=False)
    # strong rVol but price DOWN over 5m: supply absorbing the buying
    down = ignite(pc_5m=-5.0, pc_1m=-1.0)
    rows = [down] + [sol_row(address=f"Q{i}") for i in range(20)]
    ev = {e.row.address: e for e in s1.evaluate(sol_cfg(), rows, NOW)}["IGN"]
    assert "impact_eff_sign" in ev.fail_reasons


def test_holder_growth_uses_prior_snapshot_and_is_soft(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    insert_rows(conn, [ignite(ts=NOW - 300, cycle_id=0, holder=500)])
    s1 = Stage1(conn, persist=False)
    rows = [ignite(holder=505)] + [sol_row(address=f"Q{i}") for i in range(20)]  # +1% < 3% soft gate
    ev = {e.row.address: e for e in s1.evaluate(sol_cfg(), rows, NOW)}["IGN"]
    assert ev.features.holder_prev == 500 and ev.features.holder_growth_pct == pytest.approx(1.0)
    hg = [g for g in ev.gates if g.name == "holder_growth_5m"][0]
    assert hg.kind == "soft" and not hg.passed
    assert ev.passed  # soft gates never block


def test_no_lookahead_prior_rows_after_now_are_ignored(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    insert_rows(conn, [ignite(ts=NOW + 300, cycle_id=9, holder=9999)])  # future snapshot
    s1 = Stage1(conn, persist=False)
    ev = {e.row.address: e for e in s1.evaluate(sol_cfg(), [ignite(holder=500)], NOW)}["IGN"]
    assert ev.features.holder_prev is None


def test_percentile_gates_can_be_disabled(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    s1 = Stage1(conn, persist=False)
    cfg = sol_cfg(percentile_gates={}, impact_eff_min_percentile=None)
    ev = s1.evaluate(cfg, [ignite()], NOW)[0]   # single-row page: percentiles undefined
    assert ev.passed
    cfg2 = sol_cfg()
    ev2 = s1.evaluate(cfg2, [ignite()], NOW)[0]
    assert not ev2.passed and "impact_eff_pct" in ev2.fail_reasons


# ---- hourly mode (Robinhood) ------------------------------------------------------------

def test_hourly_delta_ignition_with_previous_poll(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    # previous poll 120 s ago: 3600/h -> base for 120 s = 120
    insert_rows(conn, [rh_row(ts=NOW - 120, cycle_id=0, vol_1h=3_600.0, tr_1h=60)])
    s1 = Stage1(conn, persist=False)
    rows = [rh_row(vol_1h=3_600.0 + 900.0, tr_1h=70, pc_1h=8.0)] + \
           [rh_row(address=f"0xQ{i}", vol_1h=3_600.0, tr_1h=60) for i in range(10)]
    ev = {e.row.address: e for e in s1.evaluate(rh_cfg(percentile_gates={}), rows, NOW)}["0xR"]
    f = ev.features
    assert f.mode == "hourly" and f.prev_dt_s == 120 and f.d_vol_1h == 900 and f.d_tr_1h == 10
    assert f.rvol_dt == pytest.approx(900 / 120)
    assert ev.passed, ev.fail_reasons


def test_hourly_fallback_without_previous_poll(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    s1 = Stage1(conn, persist=False)
    hot = rh_row(vol_1h_chg=350.0, tr_1h=40, pc_1h=12.0)
    cold = rh_row(address="0xC", vol_1h_chg=50.0, tr_1h=40, pc_1h=12.0)
    ev = {e.row.address: e for e in s1.evaluate(rh_cfg(percentile_gates={}), [hot, cold], NOW)}
    assert ev["0xR"].passed and "fallback_vol_1h_chg" in ev["0xC"].fail_reasons


def test_hourly_fallback_extension_veto(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    s1 = Stage1(conn, persist=False)
    late = rh_row(vol_1h_chg=5000.0, tr_1h=80, pc_1h=120.0)      # entrant already +120% on the hour
    ev = s1.evaluate(rh_cfg(fallback_price_change_1h_max_pct=80.0), [late], NOW)[0]
    assert "fallback_price_change_1h_max" in ev.fail_reasons
    ok = rh_row(vol_1h_chg=5000.0, tr_1h=80, pc_1h=40.0)
    assert s1.evaluate(rh_cfg(fallback_price_change_1h_max_pct=80.0), [ok], NOW)[0].passed


def test_hourly_vol_floor_gate(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    s1 = Stage1(conn, persist=False)
    thin = rh_row(vol_1h_chg=5000.0, tr_1h=80, pc_1h=15.0, vol_1h=900.0)
    ev = s1.evaluate(rh_cfg(vol_1h_min_usd=5000.0), [thin], NOW)[0]
    assert "vol_1h_min" in ev.fail_reasons


def test_hourly_price_band_vetoes(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    s1 = Stage1(conn, persist=False)
    ext = rh_row(vol_1h_chg=350.0, tr_1h=40, pc_1h=300.0)
    ev = s1.evaluate(rh_cfg(percentile_gates={}), [ext], NOW)[0]
    assert "price_change_1h_max" in ev.fail_reasons


def test_mode_is_data_driven_not_chain_driven():
    assert Stage1.mode_for(sol_row()) == "short"
    assert Stage1.mode_for(rh_row()) == "hourly"
    assert Stage1.mode_for(sol_row(vol_1m=None)) == "hourly"
