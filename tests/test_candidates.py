import json
from pathlib import Path

import pytest

from scanner.candidates import CandidateManager
from scanner.config import ChainConfig
from scanner.db import open_db
from scanner.ledger import CULedger, CallRecord

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
CCFG = CFG["candidates"]
NOW = 1_788_866_000
SOL = ChainConfig("solana", True, "solana", 60, {}, {})
RH = ChainConfig("robinhood", True, "robinhood", 120, {}, {})


class Clock:
    def __init__(self, t=NOW):
        self.t = float(t)

    def __call__(self):
        return self.t


def make(tmp_path, settings=None, cap=100_000, clock=None, conn=None):
    conn = conn or open_db(tmp_path / "t.sqlite")
    clock = clock or Clock()
    return CandidateManager(conn, CULedger(conn), settings or CCFG, cap, clock=clock), conn, clock


def test_cap_and_weakest_oldest_eviction(tmp_path):
    m, conn, clk = make(tmp_path)
    # 30 nominations with strength 1..30 (rvol); cap 12 -> the 12 strongest survive
    outcomes = [m.on_nomination(SOL, f"A{i}", f"S{i}", float(i), NOW + i) for i in range(1, 31)]
    assert outcomes[:12] == ["entered"] * 12 and outcomes[12:] == ["entered"] * 18   # each newcomer stronger
    act = m.active(SOL, NOW + 100)
    assert len(act) == 12 and {c.address for c in act} == {f"A{i}" for i in range(19, 31)}
    assert m.stats.evicted == 18 and m.stats.entered == 30
    # a weaker newcomer is rejected
    assert m.on_nomination(SOL, "WEAK", "W", 5.0, NOW + 200) == "rejected_cap"
    assert m.stats.rejected_cap == 1
    # tie on strength -> the OLDEST goes first
    m2, _, _ = make(tmp_path / "b")
    for i in range(12):
        m2.on_nomination(SOL, f"T{i}", None, 3.0, NOW + i)
    assert m2.on_nomination(SOL, "NEW", None, 3.5, NOW + 50) == "entered"
    assert "T0" not in {c.address for c in m2.active(SOL, NOW + 60)}
    # chains are capped independently
    assert m.on_nomination(RH, "0xR", None, 1.0, NOW + 300) == "entered"


def test_refresh_keeps_one_entry_and_persists(tmp_path):
    m, conn, _ = make(tmp_path)
    assert m.on_nomination(SOL, "A", "S", 4.0, NOW) == "entered"
    assert m.on_nomination(SOL, "A", "S", 6.0, NOW + 60) == "refreshed"
    assert len(m.active(SOL, NOW + 60)) == 1 and m.active(SOL, NOW + 60)[0].strength == 6.0
    row = conn.execute("SELECT status, last_seen_ts, state_json FROM candidates WHERE address='A'").fetchone()
    assert row["status"] == "active" and row["last_seen_ts"] == NOW + 60
    assert json.loads(row["state_json"])["symbol"] == "S"


def test_stay_and_expire(tmp_path):
    clk = Clock()
    m, conn, _ = make(tmp_path, clock=clk)
    m.on_nomination(SOL, "A", None, 4.0, NOW)
    stay_s = CCFG["max_stay_min"] * 60
    # stay evidence via a Stage-1 page row keeps it alive
    m.on_stage1_row(SOL, "A", 2.5, NOW + stay_s - 10)
    assert m.expire(NOW + stay_s + 5) == []
    # a failing stay check does not evict by itself...
    m.on_stage1_row(SOL, "A", 0.5, NOW + stay_s + 100)
    assert m.active_set[("solana", "A")].stay_fails == 1
    # ...silence does
    gone = m.expire(NOW + 2 * stay_s + 200)
    assert [c.address for c in gone] == ["A"] and m.active(SOL, NOW + 2 * stay_s + 200) == []
    assert conn.execute("SELECT status FROM candidates WHERE address='A'").fetchone()["status"] == "expired"


def test_tape_updates_strength_and_veto_blocks_reentry(tmp_path):
    m, conn, _ = make(tmp_path)
    m.on_nomination(SOL, "A", None, 4.0, NOW)
    assert m.on_tape(SOL, "A", 0.5, [], NOW + 30, anchor_ts=NOW - 60, anchor_price=1.0) is None
    c = m.active_set[("solana", "A")]
    assert c.strength == pytest.approx(4.0 * 1.5) and c.polls == 1 and c.anchor_ts == NOW - 60
    assert m.on_tape(SOL, "A", 0.1, ["WASH"], NOW + 60) == "vetoed"
    assert m.active(SOL, NOW + 60) == [] and m.stats.vetoed == 1
    assert m.on_nomination(SOL, "A", None, 9.0, NOW + 120) == "blocked_cooldown"
    assert m.on_nomination(SOL, "A", None, 9.0, NOW + 120 + CCFG["veto_cooldown_min"] * 60) == "entered"
    assert conn.execute("SELECT COUNT(*) FROM candidates WHERE address='A'").fetchone()[0] == 1


def test_negative_ofi_counts_as_stay_fail_but_strength_floors(tmp_path):
    m, _, _ = make(tmp_path)
    m.on_nomination(SOL, "A", None, 4.0, NOW)
    m.on_tape(SOL, "A", -0.95, [], NOW + 30)
    c = m.active_set[("solana", "A")]
    assert c.stay_fails == 1 and c.strength == pytest.approx(4.0 * 0.1)


def test_degrade_when_daily_cap_reached_and_recover_next_day(tmp_path):
    clk = Clock()
    m, conn, _ = make(tmp_path, cap=100, clock=clk)
    m.on_nomination(SOL, "A", None, 4.0, NOW)
    assert len(m.active(SOL, NOW)) == 1
    m.ledger.record(CallRecord("token_list_v3", "solana", 100, True, 200, 10, ts=NOW))
    assert m.active(SOL, NOW) == [] and m.stats.degraded_polls_skipped == 1
    assert m.degraded(NOW + 3600)                       # stays degraded for the day
    tomorrow = (NOW // 86400 + 1) * 86400 + 10
    clk.t = tomorrow
    assert not m.degraded(tomorrow) and len(m.active(SOL, tomorrow)) == 1   # candidate survived (still inside stay? no)


def test_restart_restores_active_and_veto_cooldown(tmp_path):
    clk = Clock()
    m, conn, _ = make(tmp_path, clock=clk)
    m.on_nomination(SOL, "A", "S", 4.0, NOW)
    m.on_nomination(SOL, "B", "T", 5.0, NOW)
    m.on_tape(SOL, "B", 0.0, ["DISTRIBUTION"], NOW + 10)
    m.on_nomination(SOL, "OLD", None, 1.0, NOW - CCFG["max_stay_min"] * 60 - 100)   # too old to survive a restart
    clk.t = NOW + 60
    m2 = CandidateManager(conn, CULedger(conn), CCFG, 100_000, clock=clk)
    assert {c.address for c in m2.active(SOL, NOW + 60)} == {"A"}
    assert m2.active_set[("solana", "A")].symbol == "S"
    assert m2.on_nomination(SOL, "B", None, 9.0, NOW + 60) == "blocked_cooldown"
    assert conn.execute("SELECT status FROM candidates WHERE address='OLD'").fetchone()["status"] == "expired"
