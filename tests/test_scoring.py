import json
from pathlib import Path

import pytest

from scanner.db import open_db
from scanner.features import TapeFeatures, WindowStats
from scanner.scoring import decide, latest_stage1_features, persist_decision, score_components, _settings
from scanner.wash import WashReport, Veto

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
SCFG = CFG["scoring"]
NOW = 1_788_866_000


def feats(*, buyers=16, sellers=8, new_share=0.5, ofi30=0.6, recent=0.3, avwap_pct=5.0, clv=True, hl=True,
          anchor_ts=NOW - 60, as_of=NOW) -> TapeFeatures:
    f = TapeFeatures(n=200, as_of=as_of, oldest_ts=NOW - 900, newest_ts=as_of, price=1.0)
    f.ofi30 = WindowStats(n=30, buyers=buyers, sellers=sellers, ofi=ofi30, new_wallet_share_usd=new_share,
                          buyer_seller_ratio=(buyers / sellers) if sellers else float("inf"))
    f.recent = WindowStats(n=10, ofi=recent)
    f.anchor_ts = anchor_ts
    f.price_vs_avwap_pct = avwap_pct
    f.clv_ge_half_2of3 = clv
    f.higher_lows_2of3 = hl
    f.bars_n = 5
    return f


def s1(eff_pct=100.0, hg=3.0):
    return {"eff_5m_pct": eff_pct, "eff_5m": 12.0, "holder_growth_pct": hg}


def wash(hard=(), soft=()):
    r = WashReport(n=60, wash_score=0.2)
    r.vetoes = [Veto(n, True) for n in hard] + [Veto(n, False) for n in soft]
    return r


def test_perfect_vector_scores_95_confirmed_and_eligible():
    d = decide("solana", "A", feats(), wash(), SCFG, NOW, s1_features=s1(), s1_ts=NOW - 30)
    by = {c.name: c for c in d.components}
    assert by["participation"].points == 30 and by["orderflow"].points == 25 and by["efficiency"].points == 15
    assert by["structure"].points == 15 and by["holder_growth"].points == 10 and by["safety"].points == 0
    assert d.score == 95 and d.tier == "CONFIRMED" and d.eligible and d.alertable
    assert d.anchor_source == "tape" and d.since_anchor_s == 60


def test_partial_vector_is_ignition():
    f = feats(buyers=10, sellers=8, new_share=0.15, ofi30=0.25, recent=0.05, avwap_pct=2.0, clv=False, hl=True)
    d = decide("solana", "A", f, wash(), SCFG, NOW, s1_features=s1(eff_pct=70, hg=1.5), s1_ts=NOW - 30)
    by = {c.name: c.points for c in d.components}
    # participation: buyers 0.5, ratio (1.25-1)/(0.3)=0.833, new 0 -> mean 0.444*30 = 13.3
    assert by["participation"] == pytest.approx(30 * (0.5 + 0.8333 + 0.0) / 3, abs=0.05)
    assert by["orderflow"] == pytest.approx(20 * 0.5)            # recent 0.05 < 0.10 -> no bonus
    assert by["efficiency"] == pytest.approx(15 * 0.5) and by["structure"] == 10 and by["holder_growth"] == 5
    assert d.score == pytest.approx(45.83, abs=0.05) and d.tier == "WATCH"     # 13.3+10+7.5+10+5


def test_mid_vector_is_ignition():
    f = feats(buyers=15, sellers=11, new_share=0.20, ofi30=0.4, recent=0.2, avwap_pct=1.0, clv=False, hl=True)
    d = decide("solana", "A", f, wash(), SCFG, NOW, s1_features=s1(eff_pct=70, hg=1.5), s1_ts=NOW - 30)
    by = {c.name: c.points for c in d.components}
    # participation: buyers 1.0, ratio (1.364-1)/0.3 -> 1.0 (clipped), new (0.2-0.15)/0.25=0.2 -> mean 0.733 -> 22
    assert by["participation"] == pytest.approx(22.0, abs=0.05)
    assert by["orderflow"] == pytest.approx(20 * 0.8 + 5)          # level 16 + recent bonus 5
    assert d.score == pytest.approx(22 + 21 + 7.5 + 10 + 5, abs=0.1) and d.tier == "IGNITION"


def test_weak_vector_is_watch():
    f = feats(buyers=3, sellers=6, new_share=0.05, ofi30=-0.2, recent=-0.3, avwap_pct=-4.0, clv=False, hl=False)
    d = decide("solana", "A", f, wash(), SCFG, NOW, s1_features=None)
    assert d.score == 0 and d.tier == "WATCH" and not d.alertable


def test_vetoes_override_score():
    d = decide("solana", "A", feats(), wash(hard=("DISTRIBUTION",), soft=("X",)), SCFG, NOW, s1_features=s1(), s1_ts=NOW)
    assert d.score == 95 and d.tier == "VETO" and not d.alertable
    assert d.hard_vetoes == ["DISTRIBUTION"] and d.soft_flags == ["X"]


@pytest.mark.parametrize("since, ok", [(20, False), (30, True), (90, True), (360, True), (361, False), (600, False)])
def test_eligibility_window(since, ok):
    d = decide("solana", "A", feats(anchor_ts=NOW - since), wash(), SCFG, NOW, s1_features=s1(), s1_ts=NOW)
    assert d.since_anchor_s == since and d.eligible is ok and d.alertable is ok


def test_anchor_falls_back_to_stage1_nomination_time():
    f = feats(anchor_ts=None)
    d = decide("solana", "A", f, wash(), SCFG, NOW, s1_features=s1(), s1_ts=NOW - 100, fallback_anchor_ts=NOW - 100)
    assert d.anchor_source == "stage1" and d.since_anchor_s == 100 and d.eligible
    d2 = decide("solana", "A", f, wash(), SCFG, NOW, s1_features=s1(), s1_ts=NOW)
    assert d2.anchor_source == "none" and d2.since_anchor_s is None and not d2.eligible


def test_since_anchor_uses_tape_as_of_not_wall_clock():
    f = feats(anchor_ts=NOW - 100, as_of=NOW - 40)     # tape newest trade 40 s ago
    d = decide("solana", "A", f, wash(), SCFG, NOW + 500, s1_features=s1(), s1_ts=NOW)
    assert d.since_anchor_s == 60


def test_components_carry_their_data_timestamps():
    d = decide("solana", "A", feats(as_of=NOW - 7), wash(), SCFG, NOW, s1_features=s1(), s1_ts=NOW - 55)
    ts = {c.name: c.data_ts for c in d.components}
    assert ts["participation"] == NOW - 7 and ts["orderflow"] == NOW - 7 and ts["structure"] == NOW - 7
    assert ts["efficiency"] == NOW - 55 and ts["holder_growth"] == NOW - 55 and ts["safety"] is None


def test_missing_stage1_features_score_zero_for_those_components():
    d = decide("solana", "A", feats(), wash(), SCFG, NOW, s1_features=None, s1_ts=None)
    by = {c.name: c.points for c in d.components}
    assert by["efficiency"] == 0 and by["holder_growth"] == 0 and d.score == 70 and d.tier == "IGNITION"


def test_persist_and_reload_stage1_features(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    conn.execute("INSERT INTO nominations(chain,address,ts,tier,features_json) VALUES('solana','A',?, 'WATCH', ?)",
                 (NOW - 30, json.dumps(s1(eff_pct=88))))
    conn.execute("INSERT INTO nominations(chain,address,ts,tier,features_json) VALUES('solana','A',?, 'CONTROL', ?)",
                 (NOW - 10, json.dumps(s1(eff_pct=1))))
    feat, ts = latest_stage1_features(conn, "solana", "A")
    assert feat["eff_5m_pct"] == 88 and ts == NOW - 30          # CONTROL rows ignored
    d = decide("solana", "A", feats(), wash(), SCFG, NOW, s1_features=feat, s1_ts=ts)
    rid = persist_decision(conn, d, tape_features_id=7)
    row = conn.execute("SELECT * FROM decisions WHERE id=?", (rid,)).fetchone()
    assert row["tier"] == "CONFIRMED" and row["alertable"] == 1 and row["tape_features_id"] == 7
    comps = json.loads(row["components_json"])
    assert all("data_ts" in c for c in comps) and comps[0]["name"] == "participation"
    assert latest_stage1_features(conn, "solana", "NONE") == (None, None)


def test_settings_merge_and_weights_sum_to_100():
    s = _settings(SCFG)
    assert sum(s["weights"].values()) == 100
    s2 = _settings({"weights": {"safety": 0}, "tier_ignition_score": 40})
    assert s2["weights"]["participation"] == 30 and s2["weights"]["safety"] == 0 and s2["tier_ignition_score"] == 40


def test_safety_verdict_integration():
    # UNSAFE -> hard veto with reasons; UNKNOWN -> CONFIRMED capped at IGNITION + soft flag; SAFE -> bonus counts
    d_unsafe = decide("solana", "A", feats(), wash(), SCFG, NOW, s1_features=s1(), s1_ts=NOW,
                      safety_verdict="UNSAFE", safety_reasons=["mint_authority", "top10"])
    assert d_unsafe.tier == "VETO" and d_unsafe.hard_vetoes == ["SAFETY:mint_authority+top10"] and not d_unsafe.alertable
    d_unknown = decide("solana", "A", feats(), wash(), SCFG, NOW, s1_features=s1(), s1_ts=NOW, safety_verdict="UNKNOWN")
    assert d_unknown.score == 95 and d_unknown.tier == "IGNITION" and "SAFETY_UNKNOWN" in d_unknown.soft_flags
    assert d_unknown.alertable                                   # IGNITION inside the window still alerts
    d_safe = decide("solana", "A", feats(), wash(), SCFG, NOW, s1_features=s1(), s1_ts=NOW,
                    safety_verdict="SAFE", safety_bonus=5.0)
    assert d_safe.score == 100 and d_safe.tier == "CONFIRMED"
    d_capped = decide("solana", "A", feats(), wash(), SCFG, NOW, s1_features=s1(), s1_ts=NOW, safety_bonus=9.0)
    assert {c.name: c.points for c in d_capped.components}["safety"] == 5   # bonus capped at the weight


def test_soft_safety_flags_cap_confirmed_at_ignition():
    d = decide("solana", "A", feats(), wash(), SCFG, NOW, s1_features=s1(), s1_ts=NOW,
               safety_verdict="SAFE", safety_bonus=3.0, safety_flags=["bundler_holdings"])
    assert d.score == 98 and d.tier == "IGNITION" and "SAFETY:bundler_holdings" in d.soft_flags and d.alertable
    d2 = decide("solana", "A", feats(), wash(), SCFG, NOW, s1_features=s1(), s1_ts=NOW,
                safety_verdict="SAFE", safety_bonus=3.0, safety_flags=["min_holders", "smart_trader_present"])
    assert d2.tier == "CONFIRMED"      # informational flags do not cap


def test_per_chain_override_raises_solana_ignition_bar_only():
    """Milestone C (2026-09-14): scoring.chains.solana.tier_ignition_score = 60; Robinhood keeps the global 55."""
    from scanner.scoring import _settings
    cfg = {**SCFG, "chains": {"solana": {"tier_ignition_score": 60}}}
    assert _settings(cfg, "solana")["tier_ignition_score"] == 60
    assert _settings(cfg, "robinhood")["tier_ignition_score"] == 55 == _settings(cfg)["tier_ignition_score"]
    assert "chains" not in _settings(cfg, "solana")
    f = feats()
    base = decide("solana", "A", f, wash(), SCFG, NOW, s1_features=s1(eff_pct=70, hg=1.5), s1_ts=NOW - 30)
    # put both bars just above this fixture's score on Solana only -> Solana drops to WATCH, Robinhood unchanged
    over = {**SCFG, "chains": {"solana": {"tier_ignition_score": base.score + 0.5, "tier_confirmed_score": base.score + 1}}}
    lo = decide("solana", "A", f, wash(), over, NOW, s1_features=s1(eff_pct=70, hg=1.5), s1_ts=NOW - 30)
    rh = decide("robinhood", "A", f, wash(), over, NOW, s1_features=s1(eff_pct=70, hg=1.5), s1_ts=NOW - 30)
    assert base.tier in ("IGNITION", "CONFIRMED") and lo.tier == "WATCH" and rh.tier == base.tier
    assert lo.score == base.score == rh.score          # the override moves the bar, never the score
