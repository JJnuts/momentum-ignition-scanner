import json
import random

import pytest

from scanner.db import open_db
from scanner.tune import (build_report, expectancy, feature_lifts, is_rug, is_win, load_events, time_to_peak, _settings)

NOW = 1_788_866_000
S = _settings(None)


def plant(conn, n=120, seed=3):
    """Synthetic dataset: nominations + controls with labels. 'planted_good' drives wins, 'noise' does not.
    Nominations win 45%, controls 15%. Winners get MFE +60% at 15m; losers MAE -30%."""
    rng = random.Random(seed)
    for i in range(n):
        kind = "control" if i % 3 == 0 else "nomination"
        good = rng.random()
        noise = rng.random()
        p_win = (0.15 if kind == "control" else 0.15 + 0.6 * good)
        win = rng.random() < p_win
        feats = {"planted_good": good, "noise": noise, "rvol_5m": 3 + 5 * good, "eff_5m_pct": 50 + 40 * good, "mode": "short"}
        cur = conn.execute("INSERT INTO nominations(chain,address,ts,tier,price,liquidity,features_json) VALUES('solana',?,?,?,?,?,?)",
                           (f"T{i}", NOW + i, "CONTROL" if kind == "control" else "WATCH", 1.0, 10_000.0, json.dumps(feats)))
        nid = cur.lastrowid
        rug = (not win) and rng.random() < 0.3
        for h in (5, 15, 30, 60):
            if win:
                close, high, low = 1 + 0.02 * h, 1.6 if h >= 15 else 1.2, 0.95
            else:
                close, high, low = 0.8, 1.05, 0.7
            liq = 10_000.0 * (0.3 if rug else 0.9)
            conn.execute("INSERT INTO labels(ref_kind,ref_id,chain,address,t0_ts,t0_price,t0_liq,horizon_min,due_ts,status,price,high,low,liquidity) "
                         "VALUES(?,?,'solana',?,?,1.0,10000,?,?, 'done',?,?,?,?)",
                         (kind, nid, f"T{i}", NOW + i, h, NOW + i + h * 60, close, high if kind == "nomination" else None,
                          low if kind == "nomination" else None, liq))


def test_dataset_join_and_metrics(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    plant(conn, n=30)
    evs = load_events(conn)
    assert len(evs) == 30 and {e.kind for e in evs} == {"nomination", "control"}
    nom = [e for e in evs if e.kind == "nomination"][0]
    assert set(nom.ret) == {5, 15, 30, 60} and set(nom.mfe) == {5, 15, 30, 60}
    assert is_win(nom, S) in (True, False) and is_rug(nom, S) in (True, False)
    ctrl = [e for e in evs if e.kind == "control"][0]
    assert ctrl.mfe == {} and is_win(ctrl, S) is not None          # controls fall back to close return


def test_planted_feature_ranks_top(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    plant(conn, n=240)
    evs = [e for e in load_events(conn) if e.kind == "nomination"]
    lifts = feature_lifts(evs, {**S, "features": ["noise", "planted_good", "rvol_5m", "eff_5m_pct", "missing"]})
    names = [f.name for f in lifts]
    assert names[0] in ("planted_good", "rvol_5m", "eff_5m_pct")     # all three are functions of the planted signal
    assert names.index("noise") == len(names) - 1 and "missing" not in names
    top = lifts[0]
    assert top.lift > 1.2 and top.win_top > top.win_bottom


def test_expectancy_and_time_to_peak(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    plant(conn, n=90)
    evs = [e for e in load_events(conn) if e.kind == "nomination"]
    ex = expectancy(evs, S)
    a, b = ex["rule_A_stop_then_time_stop_15m"], ex["rule_B_stop_then_trail_30m"]
    assert a["n"] == len(evs) and b["n"] == len(evs)
    # losers hit the -20% stop (MAE -30%), winners exit at ret_15 = +30%
    assert a["avg_loss_pct"] == pytest.approx(-20.0) and a["avg_win_pct"] == pytest.approx(30.0)
    # rule B trails: winners exit at max(ret_30=+60%, mfe_30 60% * 0.7 = 42%) = +60%
    assert b["avg_win_pct"] == pytest.approx(60.0)
    ttp = time_to_peak(evs)
    assert set(ttp) <= {5, 15, 30, 60} and sum(ttp.values()) == len(evs)


def test_report_renders_with_and_without_data(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    empty = build_report(conn, now=NOW)
    assert "# tune report" in empty and "not enough labeled" in empty
    plant(conn, n=150)
    rep = build_report(conn, now=NOW)
    for section in ("## 1. Groups", "## 2. Treatment vs control", "## 3. Feature lift within nominations",
                    "## 4. MFE / MAE percentiles", "## 5. Time to peak", "## 6. Expectancy"):
        assert section in rep, section
    assert "| planted_good |" in rep or "| rvol_5m |" in rep
    assert "lift" in rep and "nomination:" in rep and "control" in rep


def test_alert_events_pull_decision_and_tape_context(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    conn.execute("INSERT INTO nominations(chain,address,ts,tier,price,liquidity,features_json) VALUES('solana','A',?, 'WATCH',1.0,5000,?)",
                 (NOW - 60, json.dumps({"rvol_5m": 4.2})))
    conn.execute("INSERT INTO tape_features(chain,address,as_of,eval_ts,ofi30,ofi_recent,buyers30,sellers30,new_wallet_share,wash_score,features_json) "
                 "VALUES('solana','A',?,?,0.6,0.2,15,5,0.5,0.1,'{}')", (NOW, NOW))
    tf_id = conn.execute("SELECT id FROM tape_features").fetchone()[0]
    conn.execute("INSERT INTO decisions(chain,address,eval_ts,as_of,anchor_ts,anchor_source,since_anchor_s,score,tier,eligible,alertable,components_json,tape_features_id,alerted_ts) "
                 "VALUES('solana','A',?,?,?, 'tape',70,82,'CONFIRMED',1,1,'[]',?,?)", (NOW, NOW, NOW - 70, tf_id, NOW))
    conn.execute("INSERT INTO alerts(chain,address,ts,tier,score,price,liquidity,status) VALUES('solana','A',?, 'CONFIRMED',82,1.0,5000,'sent')", (NOW,))
    aid = conn.execute("SELECT id FROM alerts").fetchone()[0]
    for h in (5, 15):
        conn.execute("INSERT INTO labels(ref_kind,ref_id,chain,address,t0_ts,t0_price,t0_liq,horizon_min,due_ts,status,price,high,low,liquidity) "
                     "VALUES('alert',?,'solana','A',?,1.0,5000,?,?, 'done',1.4,1.5,0.9,4000)", (aid, NOW, h, NOW + h * 60))
    evs = [e for e in load_events(conn) if e.kind == "alert"]
    assert len(evs) == 1
    e = evs[0]
    assert e.features["score"] == 82 and e.features["ofi30"] == 0.6 and e.features["since_anchor_s"] == 70
    assert e.features["rvol_5m"] == 4.2 and e.mfe[15] == pytest.approx(50.0) and is_win(e, S) is True
