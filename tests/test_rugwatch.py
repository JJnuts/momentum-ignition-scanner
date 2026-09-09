import json
from pathlib import Path

import pytest

from scanner.config import ChainConfig
from scanner.db import open_db
from scanner.ledger import CULedger
from scanner.rugwatch import RugWatch
from scanner.safety import SafetyResult
from scanner.stage0 import ROW_COLUMNS, TokenRow

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
RCFG = CFG["rugwatch"]
NOW = 1_788_866_000
SOL = ChainConfig("solana", True, "solana", 60, {}, {})
CHAINS = {"solana": SOL}


class FakeSafety:
    def __init__(self, verdict="SAFE", reasons=()):
        self.verdict, self.reasons = verdict, list(reasons)
        self.calls = 0

    async def check(self, ch, address, force=False):
        self.calls += 1
        return SafetyResult(ch.name, address, NOW, self.verdict, [], 0.0, [], list(self.reasons), {})


class FakeClient:
    def __init__(self, liq=None):
        self.liq = liq
        self.calls = 0

    async def market_data_single(self, chain, address):
        self.calls += 1
        return {"liquidity": self.liq}


class Clock:
    def __init__(self, t=NOW):
        self.t = float(t)

    def __call__(self):
        return self.t


def row(conn, address, ts, liq):
    r = TokenRow(chain="solana", cycle_id=1, ts=ts, address=address, sort_key="x", rank=0, price=1.0, liquidity=liq)
    conn.execute(f"INSERT INTO scan_rows({', '.join(ROW_COLUMNS)}) VALUES({', '.join('?' * len(ROW_COLUMNS))})",
                 tuple(getattr(r, c) for c in ROW_COLUMNS))


def make(tmp_path, safety=None, client=None, plan="starter", clock=None):
    conn = open_db(tmp_path / "t.sqlite")
    rw = RugWatch(conn, client, CULedger(conn), safety, RCFG, plan, 100_000, clock=clock or Clock())
    return conn, rw


def test_schedule_creates_one_row_per_minute(tmp_path):
    conn, rw = make(tmp_path)
    ids = rw.schedule(1, "solana", "TOK", NOW, 50_000.0, "SAFE")
    assert len(ids) == 3
    rows = conn.execute("SELECT minute, due_ts, liq_then, safety_then, status FROM rug_checks ORDER BY minute").fetchall()
    assert [r["minute"] for r in rows] == [10, 30, 60] and [r["due_ts"] - NOW for r in rows] == [600, 1800, 3600]
    assert rows[0]["liq_then"] == 50_000.0 and rows[0]["safety_then"] == "SAFE" and rows[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_nothing_before_due_then_healthy_check_no_warning(tmp_path):
    safety = FakeSafety("SAFE")
    conn, rw = make(tmp_path, safety)
    rw.schedule(1, "solana", "TOK", NOW, 50_000.0, "SAFE")
    st = await rw.tick(CHAINS, now=NOW + 300)
    assert st.due == 0 and safety.calls == 0
    row(conn, "TOK", NOW + 590, 48_000.0)                    # free scan-row snapshot near the due time
    st = await rw.tick(CHAINS, now=NOW + 600)
    assert st.due == 1 and st.done == 1 and st.warnings == 0 and st.cu == 0 and safety.calls == 1
    r = conn.execute("SELECT status, liq_now, liq_change_pct, safety_now, warned FROM rug_checks WHERE minute=10").fetchone()
    assert r["status"] == "done" and r["liq_now"] == 48_000.0 and r["liq_change_pct"] == pytest.approx(-4.0)
    assert r["safety_now"] == "SAFE" and r["warned"] == 0 and rw.pending_warnings() == []


@pytest.mark.asyncio
async def test_liquidity_drop_warns_once_per_alert(tmp_path):
    conn, rw = make(tmp_path, FakeSafety("SAFE"))
    rw.schedule(7, "solana", "TOK", NOW, 50_000.0, "SAFE")
    row(conn, "TOK", NOW + 600, 20_000.0)                    # -60%
    st = await rw.tick(CHAINS, now=NOW + 600)
    assert st.warnings == 1
    w = rw.pending_warnings()
    assert len(w) == 1 and w[0]["reason"] == "LIQUIDITY_DROP" and "-60%" in w[0]["detail"] and w[0]["alert_id"] == 7
    # +30 still collapsed -> no second LIQUIDITY_DROP warning
    row(conn, "TOK", NOW + 1800, 15_000.0)
    st2 = await rw.tick(CHAINS, now=NOW + 1800)
    assert st2.done == 1 and st2.warnings == 0
    assert len(rw.pending_warnings()) == 1
    rw.mark_delivered(w[0]["id"], now=NOW + 1801)
    assert rw.pending_warnings() == []


@pytest.mark.asyncio
async def test_safety_flip_warns_with_reasons_and_only_once(tmp_path):
    safety = FakeSafety("SAFE")
    conn, rw = make(tmp_path, safety)
    rw.schedule(3, "solana", "TOK", NOW, 50_000.0, "SAFE")
    row(conn, "TOK", NOW + 600, 50_000.0)
    await rw.tick(CHAINS, now=NOW + 600)                     # healthy at +10
    safety.verdict, safety.reasons = "UNSAFE", ["sell_path", "sell_tax"]
    row(conn, "TOK", NOW + 1800, 50_000.0)
    st = await rw.tick(CHAINS, now=NOW + 1800)
    assert st.warnings == 1
    w = rw.pending_warnings()[0]
    assert w["reason"] == "SAFETY_FLIP" and "sell_path" in w["detail"] and w["minute"] == 30
    row(conn, "TOK", NOW + 3600, 50_000.0)
    st3 = await rw.tick(CHAINS, now=NOW + 3600)
    assert st3.warnings == 0 and len(rw.pending_warnings()) == 1


@pytest.mark.asyncio
async def test_both_reasons_combine_in_one_row(tmp_path):
    conn, rw = make(tmp_path, FakeSafety("UNSAFE", ["mint_authority"]))
    rw.schedule(9, "solana", "TOK", NOW, 50_000.0, "SAFE")
    row(conn, "TOK", NOW + 600, 10_000.0)
    st = await rw.tick(CHAINS, now=NOW + 600)
    assert st.warnings == 1 and rw.pending_warnings()[0]["reason"] == "LIQUIDITY_DROP+SAFETY_FLIP"


@pytest.mark.asyncio
async def test_market_data_fallback_and_budget(tmp_path):
    client = FakeClient(liq=25_000.0)
    conn, rw = make(tmp_path, FakeSafety("SAFE"), client)
    rw.schedule(1, "solana", "TOK", NOW, 50_000.0, "SAFE")   # no scan_rows -> market data (8 CU)
    st = await rw.tick(CHAINS, now=NOW + 600)
    assert client.calls == 1 and st.cu == 8 and st.warnings == 1        # -50%
    # free plan has no market_data -> liquidity unknown; safety alone still evaluates
    conn2, rw2 = make(tmp_path / "b", FakeSafety("SAFE"), FakeClient(liq=1.0), plan="standard")
    rw2.schedule(1, "solana", "TOK", NOW, 50_000.0, "SAFE")
    st2 = await rw2.tick(CHAINS, now=NOW + 600)
    assert st2.done == 1 and st2.warnings == 0 and st2.cu == 0


@pytest.mark.asyncio
async def test_no_data_retries_then_fails_after_grace(tmp_path):
    conn, rw = make(tmp_path, safety=None, client=None)
    rw.schedule(1, "solana", "TOK", NOW, 50_000.0, "SAFE")
    st = await rw.tick(CHAINS, now=NOW + 600)
    assert st.pending == 1 and st.done == 0
    st = await rw.tick(CHAINS, now=NOW + 600 + RCFG["grace_s"] + 1)
    assert st.failed == 1
    assert conn.execute("SELECT status FROM rug_checks WHERE minute=10").fetchone()["status"] == "failed"


@pytest.mark.asyncio
async def test_unknown_chain_rows_are_skipped(tmp_path):
    conn, rw = make(tmp_path, FakeSafety("SAFE"))
    rw.schedule(1, "other", "TOK", NOW, 1.0, "SAFE")
    st = await rw.tick(CHAINS, now=NOW + 600)
    assert st.due == 1 and st.done == 0
