"""T15c: cheapest-spend trims. Every trim must leave the signal math and veto semantics unchanged."""
import json
from pathlib import Path

import pytest

from scanner.config import ChainConfig
from scanner.db import open_db
from scanner.enrichment import Enricher
from scanner.labeler import Labeler
from scanner.ledger import CULedger
from scanner.stage0 import ROW_COLUMNS, TokenRow
from scanner.stage1 import Stage1
from scanner.tape import TapePoller, TapeStore, Trade
from scanner.wash import sell_pressure

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
NOW = 1_788_866_000
SOL = ChainConfig("solana", True, "solana", 120, CFG["chains"]["solana"]["stage0"], CFG["chains"]["solana"]["stage1"])
RH = ChainConfig("robinhood", True, "robinhood", 180, CFG["chains"]["robinhood"]["stage0"], CFG["chains"]["robinhood"]["stage1"])


def sol_row(address="A", ts=NOW, cycle_id=1, **over) -> TokenRow:
    base = dict(chain="solana", cycle_id=cycle_id, ts=ts, address=address, sort_key="x", rank=0, symbol=address,
                price=1.0, liquidity=50_000.0, market_cap=200_000.0, holder=500, listing_ts=ts - 86400,
                last_trade_ts=ts, vol_1m=2_000.0, vol_5m=6_000.0, vol_30m=9_000.0, vol_1h=12_000.0,
                pc_1m=3.0, pc_5m=15.0, pc_30m=1.0, pc_1h=25.0, tr_1m=20, tr_5m=60, tr_30m=60, tr_1h=120)
    base.update(over)
    return TokenRow(**base)


def rh_row(address="0xR", ts=NOW, cycle_id=1, **over) -> TokenRow:
    base = dict(chain="robinhood", cycle_id=cycle_id, ts=ts, address=address, sort_key="x", rank=0, symbol=address,
                price=1.0, liquidity=50_000.0, market_cap=200_000.0, holder=None, listing_ts=ts - 86400,
                last_trade_ts=ts, vol_1h=3_600.0, tr_1h=60, pc_1h=2.0)
    base.update(over)
    return TokenRow(**base)


def insert_rows(conn, rows):
    conn.executemany(f"INSERT INTO scan_rows({', '.join(ROW_COLUMNS)}) VALUES({', '.join('?' * len(ROW_COLUMNS))})",
                     [tuple(getattr(r, c) for c in ROW_COLUMNS) for r in rows])


# ---- 1. scan interval: Stage-1 math is a function of the row, not of the poll spacing ---------------

def test_config_intervals_are_the_trimmed_values():
    assert CFG["chains"]["solana"]["scan_interval_s"] == 120 and CFG["chains"]["robinhood"]["scan_interval_s"] == 180


def test_stage1_short_mode_features_identical_at_60s_and_120s_spacing(tmp_path):
    page = [sol_row(address="IGN", holder=520)] + [sol_row(address=f"Q{i}", vol_1m=100.0, vol_5m=500.0) for i in range(12)]
    out = {}
    for spacing in (60, 120):
        conn = open_db(tmp_path / f"s{spacing}.sqlite")
        # prior polls at the given spacing, holder 500 everywhere
        prior = [sol_row(address=r.address, ts=NOW - k * spacing, cycle_id=-k, holder=500)
                 for k in range(1, 8) for r in page]
        insert_rows(conn, prior)
        feats = Stage1(conn, persist=False).compute_features(SOL, page, NOW)
        out[spacing] = feats[0]
    a, b = out[60], out[120]
    assert a.rvol_1m == b.rvol_1m and a.rvol_5m == b.rvol_5m and a.eff_5m == b.eff_5m and a.base_5m == b.base_5m
    assert a.rvol_1m_pct == b.rvol_1m_pct and a.eff_5m_pct == b.eff_5m_pct
    # holder growth still finds a prior snapshot near -5 min with 120 s polls (rows at -240 / -360)
    assert b.holder_prev == 500 and b.holder_growth_pct == pytest.approx(4.0)


def test_stage1_hourly_mode_delta_works_with_180s_previous_poll(tmp_path):
    conn = open_db(tmp_path / "rh.sqlite")
    insert_rows(conn, [rh_row(ts=NOW - 180, cycle_id=0, vol_1h=3_600.0, tr_1h=60)])
    feats = Stage1(conn, persist=False).compute_features(RH, [rh_row(vol_1h=3_600.0 + 900.0, tr_1h=70)], NOW)
    f = feats[0]
    assert f.prev_dt_s == 180 and f.d_vol_1h == 900.0
    assert f.base_dt == pytest.approx(3_600.0 / 3600.0 * 180) and f.rvol_dt == pytest.approx(900.0 / 180.0)


# ---- 2. enrichment gates ---------------------------------------------------------------------------

class FakeClient:
    def __init__(self):
        self.calls: list[str] = []

    async def token_top_traders(self, chain, address, **kw):
        self.calls.append("top_traders")
        return [{"owner": "W1", "holdVolume": 5_000_000}]

    async def wallet_tags_tracker(self, chain, address, **kw):
        self.calls.append("tags")
        return {}


def seed_row(conn):
    row = TokenRow(chain="solana", cycle_id=1, ts=NOW, address="TOK", sort_key="x", rank=0, symbol="T",
                   price=0.01, liquidity=50_000.0, market_cap=1_000_000.0)
    conn.execute(f"INSERT INTO scan_rows({', '.join(ROW_COLUMNS)}) VALUES({', '.join('?' * len(ROW_COLUMNS))})",
                 tuple(getattr(row, c) for c in ROW_COLUMNS))


@pytest.mark.asyncio
async def test_holdings_fetched_only_when_seller_share_can_trigger_distribution(tmp_path):
    conn = open_db(tmp_path / "e.sqlite")
    seed_row(conn)
    client = FakeClient()
    e = Enricher(conn, client, CULedger(conn), CFG["stage2"]["enrichment"], daily_cu_cap=100_000)
    assert e.holdings_min_share == 0.6 == CFG["stage2"]["wash"]["seller_top3_share_min"]
    assert await e.holdings(SOL, "TOK", seller_top3_share=0.3) is None and client.calls == [] and e.gated_skips == 1
    assert await e.holdings(SOL, "TOK", seller_top3_share=None) is None and client.calls == []      # no sells at all
    h = await e.holdings(SOL, "TOK", seller_top3_share=0.7)
    assert h == {"W1": 5.0} and client.calls == ["top_traders"] and e.gated_skips == 2
    # legacy behaviour: gate off -> always fetch
    e2 = Enricher(conn, FakeClient(), CULedger(conn), {**CFG["stage2"]["enrichment"], "holdings_min_seller_top3_share": None}, 100_000)
    assert await e2.holdings(SOL, "TOK") == {"W1": 5.0}


@pytest.mark.asyncio
async def test_tag_flows_skipped_without_sells(tmp_path):
    conn = open_db(tmp_path / "e.sqlite")
    client = FakeClient()
    e = Enricher(conn, client, CULedger(conn), CFG["stage2"]["enrichment"], daily_cu_cap=100_000)
    assert await e.tag_flows(SOL, "TOK", now=NOW, window_sell_usd=0.0) is None and client.calls == []
    assert await e.tag_flows(SOL, "TOK", now=NOW, window_sell_usd=120.0) == {} and client.calls == ["tags"]
    assert await e.tag_flows(SOL, "TOK", now=NOW) == {}          # unknown -> fetch (cached now)


def _t(i, side, usd, wallet):
    return Trade(chain="solana", address="TOK", sig=f"s{i}", ts=NOW + i, side=side, wallet=wallet, usd=usd,
                 price=1.0, amount=usd, source="pump_amm", tx_hash=f"h{i}")


def test_sell_pressure_matches_the_wash_window():
    trades = [_t(i, "buy", 10.0, f"b{i}") for i in range(5)]
    assert sell_pressure(trades, CFG["stage2"]["wash"]) == (0.0, None)
    trades += [_t(10, "sell", 70.0, "S1"), _t(11, "sell", 20.0, "S2"), _t(12, "sell", 5.0, "S3"), _t(13, "sell", 5.0, "S4")]
    sell_usd, share = sell_pressure(trades, CFG["stage2"]["wash"])
    assert sell_usd == 100.0 and share == pytest.approx(0.95)


# ---- 3. labeler: alerts always get a candle path, nominations are sampled ---------------------------

class PathClient:
    def __init__(self):
        self.ohlcv_calls: list[tuple] = []
        self.plan_has_multi = True

    async def multi_price(self, chain, addresses, **kw):
        return {a: {"value": 1.0, "liquidity": None} for a in addresses}

    async def ohlcv_v3(self, chain, address, type_, time_from, time_to, mode="range", count_limit=None, currency="usd"):
        self.ohlcv_calls.append((address, time_from))
        return [{"unix_time": time_from + 60 * k, "o": 1.0, "h": 1.2, "l": 0.9, "c": 1.1, "v": 1.0} for k in range(62)]


def _labels(conn, kind):
    return {r["address"]: r["path_status"] for r in
            conn.execute("SELECT address, path_status FROM labels WHERE ref_kind=? AND horizon_min=60", (kind,))}


@pytest.mark.asyncio
async def test_alerts_always_get_paths_nominations_are_sampled_deterministically(tmp_path):
    settings = dict(CFG["labeler"]); settings["control_sample_per_cycle"] = 0
    assert settings["ohlcv_path_for"] == ["alert", "nomination"] and settings["nomination_path_sample"] == 0.4
    chains = {"solana": SOL}
    picked = {}
    for run in (1, 2):
        conn = open_db(tmp_path / f"l{run}.sqlite")
        client = PathClient()
        lab = Labeler(conn, client, CULedger(conn), settings, "starter", 100_000, clock=lambda: NOW + 7200)
        for i in range(40):
            lab.enqueue("nomination", i, "solana", f"N{i}", NOW, 1.0, None)
        lab.enqueue("alert", 1, "solana", "ALERTED", NOW, 1.0, None)
        st = await lab.tick(chains, now=NOW + 3600)
        noms = _labels(conn, "nomination")
        done = sorted(a for a, s in noms.items() if s == "done")
        assert set(noms.values()) <= {"done", "skipped"} and st.path_skipped_sample == len(noms) - len(done)
        assert 5 <= len(done) <= 30                                   # ~40 % of 40 (deterministic hash, not RNG)
        assert _labels(conn, "alert") == {"ALERTED": "done"}           # every alert gets its path
        assert ("ALERTED", NOW) in client.ohlcv_calls and len(client.ohlcv_calls) == len(done) + 1
        picked[run] = done
    assert picked[1] == picked[2]                                     # same events -> same sample on restart
    # sample 1.0 / legacy string keep the old behaviour
    conn = open_db(tmp_path / "legacy.sqlite")
    lab = Labeler(conn, PathClient(), CULedger(conn), {**settings, "ohlcv_path_for": "nominations", "nomination_path_sample": 1.0},
                  "starter", 100_000, clock=lambda: NOW + 7200)
    assert lab.path_kinds == ["nomination"] and lab.in_path_sample("solana", "X", NOW)
    lab.enqueue("alert", 1, "solana", "ALERTED", NOW, 1.0, None)
    lab.enqueue("nomination", 1, "solana", "N", NOW, 1.0, None)
    await lab.tick(chains, now=NOW + 3600)
    assert _labels(conn, "nomination") == {"N": "done"} and _labels(conn, "alert") == {"ALERTED": "pending"}


# ---- 4. tape: slower refresh once the eligibility window is over -------------------------------------

class TapeClient:
    def __init__(self):
        self.calls = 0

    async def txs_token_v3(self, chain, address, limit=100, offset=0, **kw):
        self.calls += 1
        return [], False


@pytest.mark.asyncio
async def test_tape_refresh_slows_after_late_after_s(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    store = TapeStore(conn)
    clk = {"t": float(NOW)}
    settings = {**CFG["tape"], "first_contact_min_span_s": 0, "daily_cu_budget": 10_000}
    assert settings["refresh_every_n_polls_late"] == 6 and settings["late_after_s"] == 420
    client = TapeClient()
    poller = TapePoller(client, store, CULedger(conn), settings, daily_cu_cap=100_000, clock=lambda: clk["t"])
    active = [(SOL, "TOK")]

    async def polls(n, step=60):
        got = 0
        for _ in range(n):
            clk["t"] += step
            got += (await poller.poll(active)).polled
        return got

    await poller.poll(active)                       # first contact at t0
    assert await polls(6) == 2                       # early: every 3rd poll -> 2 refreshes in 6 polls (t0+6 min)
    clk["t"] = NOW + 420                              # eligibility window closed
    assert await polls(12) == 2                      # late: every 6th poll -> 2 refreshes in 12 polls
    # a token that re-enters (fresh tape) is early again
    store.drop("solana", "TOK")
    await poller.poll(active)
    assert await polls(6) == 2
