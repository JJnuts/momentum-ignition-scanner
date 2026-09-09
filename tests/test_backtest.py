import gzip
import json
from pathlib import Path

import pytest

from scanner.backtest import (evaluate_paths, format_report, load_recorded_paths, recorded_safety, replay_decisions,
                              run_rules, stage1_features_as_of)
from scanner.db import open_db
from scanner.features import compute
from scanner.scoring import decide, persist_decision
from scanner.tape import TapeStore, Trade, persist_features
from scanner.wash import evaluate

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
NOW = 1_788_866_000
TOK = "TOK111111111111111111111111111111111111111"


def tape(n_quiet=180, n_burst=40):
    q = [Trade("solana", TOK, f"q{i}", NOW - 1800 + i * 10, "buy" if i % 2 == 0 else "sell", f"w{i}", 10.0, 1.0, 10.0, "x", f"q{i}")
         for i in range(n_quiet)]
    tb = q[-1].ts + 10
    b = [Trade("solana", TOK, f"b{i}", tb + i, "buy", f"B{i}", 30.0, 1.0 + 0.01 * i, 30.0 / (1.0 + 0.01 * i), "x", f"b{i}")
         for i in range(n_burst)]
    return q + b


def live_decision(conn, trades, s1=None, safety_verdict="SAFE", bonus=3.0, flags=()):
    """Persist exactly what the live path persists: trades, tape_features (+wash), nomination, decision."""
    store = TapeStore(conn)
    store.get("solana", TOK).add(trades)
    conn.executemany("INSERT OR IGNORE INTO trades(chain,address,sig,ts,side,wallet,usd,price,amount,source) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     [(t.chain, t.address, t.sig, t.ts, t.side, t.wallet, t.usd, t.price, t.amount, t.source) for t in trades])
    s1 = s1 or {"eff_5m_pct": 80.0, "holder_growth_pct": 2.0, "rvol_5m": 5.0}
    conn.execute("INSERT INTO nominations(chain,address,ts,tier,price,liquidity,features_json) VALUES('solana',?,?, 'WATCH',1.0,50000,?)",
                 (TOK, trades[-1].ts - 30, json.dumps(s1)))
    conn.execute(f"INSERT INTO scan_rows(chain,cycle_id,ts,address,sort_key,rank,price,liquidity) VALUES('solana',1,?,?,'x',0,1.0,50000)",
                 (trades[-1].ts - 30, TOK))
    fcfg, wcfg, scfg = CFG["stage2"]["features"], CFG["stage2"]["wash"], CFG["scoring"]
    f = compute(trades, fcfg)
    w = evaluate(trades, f, wcfg, liquidity=50000.0)
    eval_ts = trades[-1].ts + 5
    d = decide("solana", TOK, f, w, scfg, eval_ts, s1_features=s1, s1_ts=trades[-1].ts - 30, safety_bonus=bonus,
               safety_verdict=safety_verdict, safety_flags=list(flags))
    tf_id = persist_features(conn, "solana", TOK, f, eval_ts, wash=w)
    did = persist_decision(conn, d, tape_features_id=tf_id)
    return did, d


def test_replay_reproduces_live_decisions_exactly(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    did, d = live_decision(conn, tape())
    assert d.tier in ("IGNITION", "CONFIRMED") and d.anchor_source == "tape"
    rep = replay_decisions(conn, CFG)
    assert rep.n == 1 and rep.features_ok == 1 and rep.wash_ok == 1 and rep.decision_ok == 1 and rep.samples == []
    assert rep.decision_match_rate == 1.0


def test_replay_reports_mismatch_when_stored_decision_is_altered(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    did, d = live_decision(conn, tape())
    conn.execute("UPDATE decisions SET score=score+7, tier='WATCH' WHERE id=?", (did,))
    rep = replay_decisions(conn, CFG)
    assert rep.decision_ok == 0 and rep.features_ok == 1
    assert any("decision score" in x for x in rep.samples[0].diffs) and any("decision tier" in x for x in rep.samples[0].diffs)


def test_replay_detects_missing_trades(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    live_decision(conn, tape())
    conn.execute("DELETE FROM trades WHERE sig LIKE 'b3%'")      # live ring had trades the DB no longer has
    rep = replay_decisions(conn, CFG)
    assert rep.features_ok == 0 and any("feature n" in x for x in rep.samples[0].diffs)


def test_replay_honours_recorded_safety_inputs(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    did, d = live_decision(conn, tape(), safety_verdict="UNKNOWN", bonus=0.0)
    assert "SAFETY_UNKNOWN" in d.soft_flags and d.tier != "CONFIRMED"
    rep = replay_decisions(conn, CFG)
    assert rep.decision_ok == 1
    conn2 = open_db(tmp_path / "u.sqlite")
    did2, d2 = live_decision(conn2, tape(), safety_verdict="UNSAFE", bonus=0.0)
    assert d2.tier == "VETO"
    assert replay_decisions(conn2, CFG).decision_ok == 1
    conn3 = open_db(tmp_path / "v.sqlite")
    did3, d3 = live_decision(conn3, tape(), safety_verdict="SAFE", bonus=5.0, flags=["bundler_holdings"])
    assert d3.tier == "IGNITION"
    assert replay_decisions(conn3, CFG).decision_ok == 1


def test_stage1_features_are_bounded_by_eval_time(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    conn.execute("INSERT INTO nominations(chain,address,ts,tier,features_json) VALUES('solana','A',100,'WATCH','{\"v\":1}')")
    conn.execute("INSERT INTO nominations(chain,address,ts,tier,features_json) VALUES('solana','A',200,'WATCH','{\"v\":2}')")
    assert stage1_features_as_of(conn, "solana", "A", 150)[0] == {"v": 1}
    assert stage1_features_as_of(conn, "solana", "A", 250)[0] == {"v": 2}
    assert stage1_features_as_of(conn, "solana", "A", 50) == (None, None)


def test_recorded_safety_reconstruction():
    class R(dict):
        def __getitem__(self, k):
            return dict.get(self, k)
    comps = [{"name": "safety", "points": 3.0}]
    assert recorded_safety(R(hard_vetoes="WASH,SAFETY:mint_authority", soft_flags=""), comps) == ("UNSAFE", ["mint_authority"], [], 3.0)
    assert recorded_safety(R(hard_vetoes="", soft_flags="SAFETY_UNKNOWN"), comps) == ("UNKNOWN", [], [], 3.0)
    assert recorded_safety(R(hard_vetoes="", soft_flags="SAFETY:bundler_holdings,X"), comps) == ("SAFE", [], ["bundler_holdings"], 3.0)
    assert recorded_safety(R(hard_vetoes="", soft_flags=""), [{"name": "safety", "points": 0.0}]) == (None, [], [], 0.0)


# ---- exact-path rules ----------------------------------------------------------------------

def candles(t0, closes, spread=0.0):
    return [{"unix_time": t0 + i * 60, "o": c, "h": c + spread, "l": c - spread, "c": c} for i, c in enumerate(closes)]


def test_rules_stop_time_stop_and_trail():
    # winner: climbs to +60% by minute 20, then fades to +30% at 30; rule A exits at +15 (=+45%), rule B trails from the peak
    path = [1.0 + 0.03 * i for i in range(21)] + [1.6 - 0.03 * (i + 1) for i in range(10)]
    a, b, hit, peak, pm = run_rules(candles(NOW, path), 1.0, 20.0, 30.0)
    assert not hit and a == pytest.approx(45.0) and peak == pytest.approx(60.0) and pm == 20
    # trail level = 1.60*0.7 = 1.12; the fade only reaches 1.30 by minute 30 -> time exit at +30%
    assert b == pytest.approx(30.0)
    # loser: stop hit at minute 3
    a2, b2, hit2, _, _ = run_rules(candles(NOW, [1.0, 0.95, 0.9, 0.75, 0.7]), 1.0, 20.0, 30.0)
    assert hit2 and a2 == -20.0 and b2 == -20.0
    # ambiguous candle (new high AND stop in one bar) counts as the stop
    amb = [{"unix_time": NOW, "o": 1.0, "h": 1.5, "l": 0.7, "c": 1.2}]
    assert run_rules(amb, 1.0, 20.0, 30.0)[2] is True
    # short path with no exit -> last close
    assert run_rules(candles(NOW, [1.0, 1.1]), 1.0, 20.0, 30.0)[0] == pytest.approx(10.0)
    assert run_rules([], 1.0, 20.0, 30.0) == (0.0, 0.0, False, 0.0, 0)


def test_evaluate_paths_uses_recorded_ohlcv(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    raw = tmp_path / "raw"; raw.mkdir()
    rec = {"ts": NOW + 3600, "endpoint": "ohlcv_v3", "chain": "solana", "status": 200,
           "params": {"address": TOK, "type": "1m", "time_from": NOW, "time_to": NOW + 3600},
           "payload": {"data": {"items": candles(NOW, [1.0 + 0.02 * i for i in range(61)])}}}
    with gzip.open(raw / "2026-09-09.jsonl.gz", "at", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
        fh.write("not json\n")
    conn.execute("INSERT INTO labels(ref_kind,ref_id,chain,address,t0_ts,t0_price,horizon_min,due_ts,status,path_status) "
                 "VALUES('alert',1,'solana',?,?,1.0,60,?,'done','done')", (TOK, NOW, NOW + 3600))
    conn.execute("INSERT INTO labels(ref_kind,ref_id,chain,address,t0_ts,t0_price,horizon_min,due_ts,status,path_status) "
                 "VALUES('nomination',2,'solana','OTHER',?,1.0,60,?,'done','pending')", (NOW, NOW + 3600))
    assert len(load_recorded_paths(raw)) == 1
    out = evaluate_paths(conn, raw, CFG)
    assert len(out) == 1 and out[0].kind == "alert" and out[0].rule_a_pct == pytest.approx(30.0)
    txt = format_report(replay_decisions(conn, CFG), out)
    assert "Reproducibility" in txt and "alert: n=1" in txt and "nomination: no recorded paths" in txt


def test_knowledge_time_filter_reproduces_live_after_backfill(tmp_path):
    """A trade with an EARLIER block time that we only learned about AFTER the decision must be excluded."""
    conn = open_db(tmp_path / "t.sqlite")
    trades = tape()
    did, d = live_decision(conn, trades)
    conn.execute("UPDATE trades SET ingested_ts=?", (d.eval_ts - 1,))          # everything known before the decision
    late = Trade("solana", TOK, "late1", trades[-5].ts, "buy", "LATE", 500.0, 1.2, 400.0, "x", "late1")
    conn.execute("INSERT INTO trades(chain,address,sig,ts,side,wallet,usd,price,amount,source,ingested_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (late.chain, late.address, late.sig, late.ts, late.side, late.wallet, late.usd, late.price, late.amount, late.source, d.eval_ts + 300))
    rep = replay_decisions(conn, CFG)
    assert rep.features_ok == 1 and rep.decision_ok == 1 and rep.legacy_no_knowledge == 0
    # without knowledge time (legacy rows) the backfilled trade leaks in and the mismatch is reported
    conn.execute("UPDATE trades SET ingested_ts=NULL")
    rep2 = replay_decisions(conn, CFG)
    assert rep2.features_ok == 0 and rep2.legacy_no_knowledge == 1


def test_config_version_is_used_for_replay(tmp_path):
    from scanner.db import register_config
    conn = open_db(tmp_path / "t.sqlite")
    old_cfg = json.loads(json.dumps(CFG))
    old_cfg["scoring"]["eligible_after_anchor_s"] = [30, 60]        # a narrower window that was live at the time
    h = register_config(conn, old_cfg, NOW)
    trades = tape()
    store = TapeStore(conn); store.get("solana", TOK).add(trades)
    conn.executemany("INSERT OR IGNORE INTO trades(chain,address,sig,ts,side,wallet,usd,price,amount,source,ingested_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     [(t.chain, t.address, t.sig, t.ts, t.side, t.wallet, t.usd, t.price, t.amount, t.source, NOW) for t in trades])
    fcfg, wcfg = old_cfg["stage2"]["features"], old_cfg["stage2"]["wash"]
    f = compute(trades, fcfg); w = evaluate(trades, f, wcfg)
    eval_ts = trades[-1].ts + 5
    d = decide("solana", TOK, f, w, old_cfg["scoring"], eval_ts, safety_verdict="SAFE", safety_bonus=3.0)
    tf_id = persist_features(conn, "solana", TOK, f, eval_ts, wash=w)
    persist_decision(conn, d, tape_features_id=tf_id, config_hash=h)
    rep = replay_decisions(conn, CFG)              # current config has a wider window -> would differ without versioning
    assert rep.exact_n == 1 and rep.exact_decision_ok == 1 and rep.legacy_no_config == 0
    txt = format_report(rep, [])
    assert "exactly reproducible subset" in txt and "1/1 (100.0%)" in txt
