import json
from pathlib import Path

import pytest

from scanner.birdeye import BirdeyeError, EndpointUnavailable
from scanner.config import ChainConfig
from scanner.db import column_names, open_db
from scanner.labeler import Labeler
from scanner.ledger import CULedger
from scanner.stage0 import ROW_COLUMNS, TokenRow
from scanner.stage1 import Stage1

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
NOW = 1_788_866_000
# T15c path kinds / sampling are covered in test_trims.py; these tests use the legacy nomination-only path fill
SETTINGS = {**CFG["labeler"], "ohlcv_path_for": "nominations", "nomination_path_sample": 1.0}


def sol_cfg() -> ChainConfig:
    s1 = dict(CFG["chains"]["solana"]["stage1"])
    s1.update(rvol_1m_min=4.0, rvol_5m_min=3.0, percentile_gates={}, impact_eff_min_percentile=None)
    return ChainConfig("solana", True, "solana", 60, CFG["chains"]["solana"]["stage0"], s1)


def sol_row(address="A", ts=NOW, cycle_id=1, **over) -> TokenRow:
    base = dict(chain="solana", cycle_id=cycle_id, ts=ts, address=address, sort_key="x", rank=0, symbol=address,
                price=1.0, liquidity=50_000.0, market_cap=200_000.0, holder=500, listing_ts=ts - 86400,
                last_trade_ts=ts, vol_1m=100.0, vol_5m=500.0, vol_30m=3_000.0, vol_1h=6_000.0,
                pc_1m=0.1, pc_5m=0.5, pc_30m=1.0, pc_1h=2.0, tr_1m=2, tr_5m=10, tr_30m=60, tr_1h=120)
    base.update(over)
    return TokenRow(**base)


def ignite(address="IGN", **over) -> TokenRow:
    d = dict(vol_1m=2_000.0, vol_5m=6_000.0, vol_30m=9_000.0, vol_1h=12_000.0,
             pc_1m=3.0, pc_5m=15.0, pc_1h=25.0, tr_1m=20, tr_5m=60, price=2.0)
    d.update(over)
    return sol_row(address=address, **d)


def insert_rows(conn, rows):
    conn.executemany(f"INSERT INTO scan_rows({', '.join(ROW_COLUMNS)}) VALUES({', '.join('?' * len(ROW_COLUMNS))})",
                     [tuple(getattr(r, c) for c in ROW_COLUMNS) for r in rows])


class FakeClient:
    """Scripted multi_price / ohlcv_v3 with a session_cu-style ledger hook."""

    def __init__(self, prices=None, candles=None, plan_has_multi=True, ohlcv_error=None):
        self.prices = prices or {}
        self.candles = candles if candles is not None else []
        self.plan_has_multi = plan_has_multi
        self.ohlcv_error = ohlcv_error
        self.multi_calls: list[list[str]] = []
        self.ohlcv_calls: list[tuple] = []

    async def multi_price(self, chain, addresses, include_liquidity=True):
        if not self.plan_has_multi:
            raise EndpointUnavailable("multi_price", None, "gated")
        self.multi_calls.append(list(addresses))
        return {a: {"value": self.prices[a][0], "liquidity": self.prices[a][1]} for a in addresses if a in self.prices}

    async def ohlcv_v3(self, chain, address, type_, time_from, time_to, mode="range", count_limit=None, currency="usd"):
        self.ohlcv_calls.append((chain, address, type_, time_from, time_to))
        if self.ohlcv_error:
            raise self.ohlcv_error
        return list(self.candles)


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, client=None, plan="lite", settings=None, cap=10_000, clock=None):
    conn = open_db(tmp_path / "t.sqlite")
    clock = clock or Clock()
    # tests that don't ask for controls get none, so label counts are exact
    lab = Labeler(conn, client, CULedger(conn), settings or {**SETTINGS, "control_sample_per_cycle": 0}, plan, cap, clock=clock)
    return conn, lab, clock


def nominate(conn, rows, now=NOW, cycle_id=1):
    s1 = Stage1(conn, persist=True)
    return s1.run_cycle(sol_cfg(), rows, now, cycle_id)


# ---- schema / enqueue ---------------------------------------------------------------

def test_schema_v3_label_columns(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    cols = column_names(conn, "labels")
    for c in ("attempts", "path_status", "source"):
        assert c in cols


def test_on_cycle_enqueues_nominations_and_controls_with_seeded_rng(tmp_path):
    conn, lab, _ = make(tmp_path, settings={**SETTINGS, "control_sample_per_cycle": 2})
    rows = [ignite()] + [sol_row(address=f"Q{i}") for i in range(10)]
    s1 = nominate(conn, rows)
    assert len(s1.nominated_rows) == 1 and s1.nominated_rows[0][0] is not None
    st = lab.on_cycle(sol_cfg(), s1, NOW, 1)
    assert st.nominations_enqueued == 1 and st.controls_drawn == 2
    assert st.labels_created == 3 * 4
    # nomination labels reference the nomination id, with t0 price/liq from the row
    nom_id = s1.nominated_rows[0][0]
    rows_db = conn.execute("SELECT horizon_min, due_ts, t0_price, t0_liq FROM labels WHERE ref_kind='nomination' "
                           "AND ref_id=? ORDER BY horizon_min", (nom_id,)).fetchall()
    assert [r["horizon_min"] for r in rows_db] == [5, 15, 30, 60]
    assert [r["due_ts"] - NOW for r in rows_db] == [300, 900, 1800, 3600]
    assert rows_db[0]["t0_price"] == 2.0 and rows_db[0]["t0_liq"] == 50_000.0
    # controls are CONTROL-tier nominations (with features) and never the nominated token
    ctrl = conn.execute("SELECT address, features_json FROM nominations WHERE tier='CONTROL'").fetchall()
    assert len(ctrl) == 2 and all(r["address"] != "IGN" for r in ctrl)
    assert json.loads(ctrl[0]["features_json"])["mode"] == "short"
    # same seed -> same draw
    conn2, lab2, _ = make(tmp_path / "b", settings={**SETTINGS, "control_sample_per_cycle": 2})
    s1b = nominate(conn2, rows)
    lab2.on_cycle(sol_cfg(), s1b, NOW, 1)
    ctrl2 = conn2.execute("SELECT address FROM nominations WHERE tier='CONTROL' ORDER BY id").fetchall()
    assert [r["address"] for r in ctrl] == [r["address"] for r in ctrl2]


def test_fractional_control_rate_is_deterministic_per_cycle(tmp_path):
    conn, lab, _ = make(tmp_path, settings={**SETTINGS, "control_sample_per_cycle": 0.5})
    draws = [lab._draw_count("solana", c) for c in range(200)]
    assert set(draws) <= {0, 1} and 60 < sum(draws) < 140
    assert draws == [lab._draw_count("solana", c) for c in range(200)]


# ---- close pass ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshots_fire_only_when_due_and_prefer_free_scan_rows(tmp_path):
    client = FakeClient(prices={"IGN": (3.0, 40_000.0)})
    conn, lab, clock = make(tmp_path, client)
    s1 = nominate(conn, [ignite()] + [sol_row(address=f"Q{i}") for i in range(10)])
    lab.on_cycle(sol_cfg(), s1, NOW, 1)
    chains = {"solana": sol_cfg()}
    # before anything is due: nothing happens
    ts = await lab.tick(chains, now=NOW + 200)
    assert ts.due == 0 and client.multi_calls == []
    # +5m due; a scan_row snapshot exists 30 s before -> free
    insert_rows(conn, [ignite(ts=NOW + 270, cycle_id=5, price=2.5, liquidity=45_000.0)])
    ts = await lab.tick(chains, now=NOW + 300)
    assert ts.due == 1 and ts.done_scan_row == 1 and client.multi_calls == []
    r = conn.execute("SELECT status, price, liquidity, source FROM labels WHERE horizon_min=5").fetchone()
    assert r["status"] == "done" and r["price"] == 2.5 and r["liquidity"] == 45_000.0 and r["source"] == "scan_row"
    # +15m due; no scan_row -> multi_price batch
    ts = await lab.tick(chains, now=NOW + 900)
    assert ts.done_multi_price == 1 and client.multi_calls == [["IGN"]]
    r = conn.execute("SELECT status, price, liquidity, source FROM labels WHERE horizon_min=15").fetchone()
    assert r["status"] == "done" and r["price"] == 3.0 and r["source"] == "multi_price"


@pytest.mark.asyncio
async def test_unresolvable_label_retries_then_fails_after_grace(tmp_path):
    client = FakeClient(prices={})   # token vanished
    conn, lab, _ = make(tmp_path, client)
    s1 = nominate(conn, [ignite()] + [sol_row(address=f"Q{i}") for i in range(10)])
    lab.on_cycle(sol_cfg(), s1, NOW, 1)
    chains = {"solana": sol_cfg()}
    ts = await lab.tick(chains, now=NOW + 300)
    assert ts.still_pending == 1 and ts.failed == 0
    assert conn.execute("SELECT attempts FROM labels WHERE horizon_min=5").fetchone()["attempts"] == 1
    ts = await lab.tick(chains, now=NOW + 300 + SETTINGS["grace_s"] + 1)
    assert ts.failed == 1
    assert conn.execute("SELECT status FROM labels WHERE horizon_min=5").fetchone()["status"] == "failed"


@pytest.mark.asyncio
async def test_free_plan_has_no_multi_price_and_keeps_pending(tmp_path):
    client = FakeClient(prices={"IGN": (3.0, None)}, plan_has_multi=False)
    conn, lab, _ = make(tmp_path, client, plan="standard")
    s1 = nominate(conn, [ignite()] + [sol_row(address=f"Q{i}") for i in range(10)])
    lab.on_cycle(sol_cfg(), s1, NOW, 1)
    ts = await lab.tick({"solana": sol_cfg()}, now=NOW + 300)
    assert ts.done_multi_price == 0 and ts.still_pending == 1 and client.multi_calls == []


@pytest.mark.asyncio
async def test_pending_labels_survive_restart(tmp_path):
    client = FakeClient(prices={"IGN": (3.0, 40_000.0)})
    conn, lab, _ = make(tmp_path, client)
    s1 = nominate(conn, [ignite()] + [sol_row(address=f"Q{i}") for i in range(10)])
    lab.on_cycle(sol_cfg(), s1, NOW, 1)
    # "restart": brand-new Labeler on the same DB, no in-memory state carried over
    lab2 = Labeler(conn, client, CULedger(conn), {**SETTINGS, "control_sample_per_cycle": 0}, "lite", 10_000,
                   clock=Clock(NOW + 900))
    ts = await lab2.tick({"solana": sol_cfg()})
    assert ts.due == 2 and ts.done_multi_price == 2   # +5 and +15 both due by now


# ---- path pass ------------------------------------------------------------------------

def candles(t0, closes, spread=0.1):
    return [{"unix_time": t0 + i * 60, "o": c, "h": c + spread, "l": c - spread, "c": c, "v": 1, "v_usd": 1}
            for i, c in enumerate(closes)]


@pytest.mark.asyncio
async def test_path_pass_fills_mfe_mae_for_all_horizons_at_final_horizon(tmp_path):
    closes = [2.0] * 60
    closes[3] = 2.6      # spike inside +5
    closes[10] = 1.5     # dip inside +15
    closes[40] = 3.0     # peak inside +60
    client = FakeClient(prices={"IGN": (2.1, 40_000.0), "Q0": (1.0, None), "Q1": (1.0, None), "Q2": (1.0, None),
                                "Q3": (1.0, None), "Q4": (1.0, None), "Q5": (1.0, None), "Q6": (1.0, None),
                                "Q7": (1.0, None), "Q8": (1.0, None), "Q9": (1.0, None)},
                        candles=candles(NOW, closes))
    conn, lab, _ = make(tmp_path, client, settings={**SETTINGS, "control_sample_per_cycle": 1})
    s1 = nominate(conn, [ignite()] + [sol_row(address=f"Q{i}") for i in range(10)])
    lab.on_cycle(sol_cfg(), s1, NOW, 1)
    chains = {"solana": sol_cfg()}
    ts = await lab.tick(chains, now=NOW + 1800)      # +60 not yet due -> no ohlcv
    assert ts.path_due == 0 and client.ohlcv_calls == []
    ts = await lab.tick(chains, now=NOW + 3600)
    assert ts.path_due == 1 and ts.path_done == 1 and len(client.ohlcv_calls) == 1
    assert client.ohlcv_calls[0][3:] == (NOW, NOW + 3600)
    got = {r["horizon_min"]: (r["high"], r["low"], r["price"], r["path_status"], r["status"]) for r in
           conn.execute("SELECT horizon_min, high, low, price, path_status, status FROM labels WHERE ref_kind='nomination'")}
    assert got[5][:2] == pytest.approx((2.7, 1.9))          # spike at minute 3 inside +5
    assert got[15][:2] == pytest.approx((2.7, 1.4))         # dip at minute 10 inside +15
    assert got[60][:2] == pytest.approx((3.1, 1.4))         # peak at minute 40
    assert all(v[3] == "done" and v[4] == "done" for v in got.values())
    # close prices came from multi_price for +5/15/30/60 (all due by now) -> price not overwritten by candle close
    assert got[5][2] == 2.1
    # controls never get a path
    ctrl = conn.execute("SELECT path_status FROM labels WHERE ref_kind='control'").fetchall()
    assert ctrl and all(r["path_status"] == "pending" for r in ctrl)
    assert lab.ledger.session_cu == 0  # FakeClient doesn't charge; ledger untouched


@pytest.mark.asyncio
async def test_path_pass_retries_then_gives_up(tmp_path):
    client = FakeClient(prices={"IGN": (2.1, None)}, ohlcv_error=BirdeyeError("ohlcv_v3", 500, "down"))
    conn, lab, _ = make(tmp_path, client, settings={**SETTINGS, "max_path_attempts": 2})
    s1 = nominate(conn, [ignite()] + [sol_row(address=f"Q{i}") for i in range(10)])
    lab.on_cycle(sol_cfg(), s1, NOW, 1)
    chains = {"solana": sol_cfg()}
    await lab.tick(chains, now=NOW + 3600)
    await lab.tick(chains, now=NOW + 3630)
    ts = await lab.tick(chains, now=NOW + 3660)
    assert len(client.ohlcv_calls) == 2 and ts.path_failed == 1
    assert conn.execute("SELECT DISTINCT path_status FROM labels WHERE ref_kind='nomination'").fetchone()[0] == "failed"


@pytest.mark.asyncio
async def test_path_pass_respects_daily_cu_cap(tmp_path):
    client = FakeClient(prices={"IGN": (2.1, None)}, candles=candles(NOW, [2.0] * 60))
    conn, lab, _ = make(tmp_path, client, cap=10)     # < 45 CU
    s1 = nominate(conn, [ignite()] + [sol_row(address=f"Q{i}") for i in range(10)])
    lab.on_cycle(sol_cfg(), s1, NOW, 1)
    ts = await lab.tick({"solana": sol_cfg()}, now=NOW + 3600)
    assert ts.path_skipped_budget == 1 and client.ohlcv_calls == []


def test_apply_path_on_real_candle_shape(tmp_path):
    conn, lab, _ = make(tmp_path)
    ids = lab.enqueue("nomination", 1, "solana", "X", NOW, 1.0, None)
    real = [{"o": 1.0, "h": 1.2, "l": 0.9, "c": 1.1, "v": 10, "v_usd": 10, "address": "X", "type": "1m",
             "unix_time": NOW + 60 * i, "currency": "usd"} for i in range(60)]
    out = lab.apply_path("nomination", 1, "solana", "X", NOW, real)
    assert out[5] == (1.2, 0.9, 1.1) and out[60] == (1.2, 0.9, 1.1)
    assert conn.execute("SELECT COUNT(*) FROM labels WHERE path_status='done'").fetchone()[0] == 4
    assert lab.counts() == {"done/done": 4}


def test_path_fill_recovers_a_failed_close_label(tmp_path):
    conn, lab, _ = make(tmp_path)
    lab.enqueue("nomination", 7, "solana", "X", NOW, 1.0, None)
    conn.execute("UPDATE labels SET status='failed', attempts=3 WHERE horizon_min=5")
    lab.apply_path("nomination", 7, "solana", "X", NOW, candles(NOW, [1.0] * 60))
    r = conn.execute("SELECT status, price, source FROM labels WHERE horizon_min=5").fetchone()
    assert r["status"] == "done" and r["price"] == 1.0 and r["source"] == "ohlcv"
