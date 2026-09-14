"""Near-miss ("why not") log."""
import json
from pathlib import Path

import pytest

from scanner.config import ChainConfig
from scanner.db import EXPECTED_TABLES, open_db
from scanner.labeler import Labeler
from scanner.ledger import CULedger
from scanner.nearmiss import NearMissLog, classify
from scanner.scoring import Component, Decision
from scanner.tune import build_report, load_events

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
NOW = 1_788_866_000
SOL = ChainConfig("solana", True, "solana", 120, {}, {})


def dec(score=58.0, tier="WATCH", eligible=True, alertable=False, hard=(), since=120):
    return Decision(chain="solana", address="TOK", eval_ts=NOW, as_of=NOW, anchor_ts=NOW - since, anchor_source="tape",
                    since_anchor_s=since, score=score, tier=tier, eligible=eligible, alertable=alertable,
                    hard_vetoes=list(hard), soft_flags=[], components=[Component("participation", 20, 30)])


def test_classify_every_kind_and_the_non_cases():
    bar, mx = 60.0, 360
    s = classify(dec(score=55.0), bar, mx)
    assert s and (s.kind, s.reason, s.margin) == ("score", "score", 5.0)
    assert classify(dec(score=49.9), bar, mx) is None                              # beyond the margin
    v = classify(dec(score=66.0, tier="VETO", hard=("WASH",)), bar, mx)
    assert v and (v.kind, v.reason, v.margin) == ("veto", "WASH", 6.0)
    sv = classify(dec(score=66.0, tier="VETO", hard=("SAFETY:mint_authority",)), bar, mx)
    assert sv and sv.reason == "SAFETY"
    assert classify(dec(score=66.0, tier="VETO", hard=("WASH", "DISTRIBUTION")), bar, mx) is None   # two vetoes
    assert classify(dec(score=55.0, tier="VETO", hard=("WASH",)), bar, mx) is None                  # veto AND under bar
    p = classify(dec(score=70.0, tier="IGNITION", alertable=True), bar, mx, alert_outcome="cooldown")
    assert p and (p.kind, p.reason) == ("policy", "cooldown")
    assert classify(dec(score=70.0, tier="IGNITION", alertable=True), bar, mx, alert_outcome="sent") is None
    late = classify(dec(score=70.0, tier="IGNITION", eligible=False, since=500), bar, mx)
    assert late and (late.kind, late.margin) == ("late", 140.0)
    assert classify(dec(score=70.0, tier="IGNITION", eligible=False, since=10), bar, mx) is None    # early, not late
    assert classify(dec(score=40.0, eligible=False, since=500), bar, mx) is None


def test_schema_has_near_misses():
    assert "near_misses" in EXPECTED_TABLES


def test_log_records_dedupes_caps_and_enqueues_labels(tmp_path):
    conn = open_db(tmp_path / "n.sqlite")
    clk = {"t": float(NOW)}
    lab = Labeler(conn, None, CULedger(conn), {**CFG["labeler"], "control_sample_per_cycle": 0}, "starter", 100_000,
                  clock=lambda: clk["t"])
    nml = NearMissLog(conn, {**CFG["near_miss"], "daily_max": 2}, labeler=lab, clock=lambda: clk["t"])
    nm = classify(dec(score=55.0), 60.0, 360)
    rid = nml.record("solana", "TOK", 1, nm, price=1.0, liquidity=5_000.0)
    assert rid is not None
    assert nml.record("solana", "TOK", 2, nm) is None and nml.stats["dedup"] == 1        # same reason inside cooldown
    other = classify(dec(score=66.0, tier="VETO", hard=("WASH",)), 60.0, 360)
    assert nml.record("solana", "TOK", 3, other) is not None                              # different reason -> new row
    assert nml.record("solana", "OTHER", 4, nm) is None and nml.stats["daily_max"] == 1   # daily cap
    clk["t"] += 25 * 60
    rows = conn.execute("SELECT kind, reason, score, margin, vetoes FROM near_misses ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("score", "score", 55.0, 5.0, None), ("veto", "WASH", 66.0, 6.0, "WASH")]
    labels = conn.execute("SELECT ref_kind, ref_id, horizon_min FROM labels ORDER BY id").fetchall()
    assert len(labels) == 8 and {r["ref_kind"] for r in labels} == {"near_miss"} and {r["ref_id"] for r in labels} == {1, 2}
    assert lab.path_samples["near_miss"] == CFG["labeler"]["near_miss_path_sample"] == 0.4
    assert "near_miss" in lab.path_kinds
    disabled = NearMissLog(conn, {"enabled": False})
    assert disabled.record("solana", "X", 9, nm) is None


def test_tune_report_has_a_why_not_section(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    # 12 WASH-vetoed near misses that all won (+40% at 15m), 12 score near misses that all lost, 12 controls that lost
    for i in range(36):
        if i < 24:
            reason = "WASH" if i < 12 else "score"
            kind = "veto" if i < 12 else "score"
            cur = conn.execute("INSERT INTO near_misses(chain,address,ts,kind,reason,score,tier,price,liquidity) "
                               "VALUES('solana',?,?,?,?,62,'VETO',1.0,10000)", (f"N{i}", NOW + i, kind, reason))
            ref_kind, ref_id, win = "near_miss", cur.lastrowid, (i < 12)
        else:
            cur = conn.execute("INSERT INTO nominations(chain,address,ts,tier,price,liquidity,features_json) "
                               "VALUES('solana',?,?,'CONTROL',1.0,10000,'{}')", (f"C{i}", NOW + i))
            ref_kind, ref_id, win = "control", cur.lastrowid, False
        for h in (5, 15, 30, 60):
            close, high, low = (1.3, 1.4, 0.98) if win else (0.85, 1.02, 0.8)
            conn.execute("INSERT INTO labels(ref_kind,ref_id,chain,address,t0_ts,t0_price,t0_liq,horizon_min,due_ts,status,price,high,low,liquidity) "
                         "VALUES(?,?,'solana','x',?,1.0,10000,?,?,'done',?,?,?,9000)",
                         (ref_kind, ref_id, NOW + i, h, NOW + i + h * 60, close, high if ref_kind == "near_miss" else None,
                          low if ref_kind == "near_miss" else None))
    evs = load_events(conn)
    nms = [e for e in evs if e.kind == "near_miss"]
    assert len(nms) == 24 and nms[0].features["nm_reason"] in ("WASH", "score") and nms[0].mfe
    text = build_report(conn, None, now=NOW)
    assert "## 7. Near misses" in text
    assert "| veto:WASH | 12 |" in text and "discarding winners" in text.lower()
    assert "| score | 12 |" in text and "correctly excluded" in text
