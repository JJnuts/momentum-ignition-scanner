"""Stage-2 scoring, tiering and decision timing (T9; SPEC s10/s11).

score (0..100) = participation 30 + order flow 25 + impact efficiency 15 +
structure 15 + holder growth 10 + safety bonus 5. Each component maps its
inputs linearly between a `lo` (0) and a `hi` (full marks) from config.
Vetoes always win: any hard veto -> tier VETO regardless of score.

Tiers: CONFIRMED >= 75, IGNITION >= 55, else WATCH.
Eligibility: seconds since the anchor within [min, max]. The anchor is the
tape's ignition onset when found, else the Stage-1 nomination time
(anchor_source records which). Every component carries the data timestamp
it used, so a decision is reproducible from stored data.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

from .features import TapeFeatures
from .wash import WashReport

DEFAULTS: dict[str, Any] = {
    "weights": {"participation": 30, "orderflow": 25, "efficiency": 15, "structure": 15, "holder_growth": 10, "safety": 5},
    "participation": {"buyers_lo": 5, "buyers_hi": 15, "ratio_lo": 1.0, "ratio_hi": 1.3,
                      "new_wallet_lo": 0.15, "new_wallet_hi": 0.40},
    "orderflow": {"ofi_lo": 0.0, "ofi_hi": 0.5, "recent_min": 0.10, "recent_points": 5},
    "efficiency": {"pct_lo": 40, "pct_hi": 100},
    "structure": {"avwap_points": 5, "clv_points": 5, "higher_lows_points": 5},
    "holder_growth": {"pct_lo": 0.0, "pct_hi": 3.0},
    "tier_ignition_score": 55,
    "tier_confirmed_score": 75,
    "eligible_after_anchor_s": [30, 180],
}


# soft safety flags that cap the tier at IGNITION (SPEC s6: bundle -> cap at IGNITION)
CAP_FLAGS = {"bundler_holdings", "sniper_holdings", "token2022_extensions"}


def _settings(cfg: dict[str, Any] | None) -> dict[str, Any]:
    s = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for k, v in (cfg or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(s.get(k), dict):
            s[k].update(v)
        else:
            s[k] = v
    return s


def _lin(x: float | None, lo: float, hi: float) -> float:
    if x is None:
        return 0.0
    if hi == lo:
        return 1.0 if x >= hi else 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


@dataclass
class Component:
    name: str
    points: float
    max_points: float
    inputs: dict[str, Any] = field(default_factory=dict)
    data_ts: int | None = None     # timestamp of the data this component used


@dataclass
class Decision:
    chain: str
    address: str
    eval_ts: int
    as_of: int | None
    anchor_ts: int | None
    anchor_source: str            # tape | stage1 | none
    since_anchor_s: int | None
    score: float
    tier: str                     # VETO | CONFIRMED | IGNITION | WATCH
    eligible: bool
    alertable: bool
    hard_vetoes: list[str]
    soft_flags: list[str]
    components: list[Component]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def score_components(f: TapeFeatures, s1: dict[str, Any] | None, s1_ts: int | None, s: dict[str, Any],
                     safety_bonus: float = 0.0) -> list[Component]:
    w = s["weights"]
    comps: list[Component] = []
    # participation breadth (tape)
    p = s["participation"]
    win = f.ofi30
    ratio = win.buyer_seller_ratio
    sub = [_lin(win.buyers, p["buyers_lo"], p["buyers_hi"]),
           _lin(min(ratio, 99.0) if ratio is not None else None, p["ratio_lo"], p["ratio_hi"]),
           _lin(win.new_wallet_share_usd, p["new_wallet_lo"], p["new_wallet_hi"])]
    comps.append(Component("participation", w["participation"] * sum(sub) / 3, w["participation"],
                           {"buyers": win.buyers, "sellers": win.sellers, "buyer_seller_ratio": ratio,
                            "new_wallet_share": win.new_wallet_share_usd, "sub": [round(x, 3) for x in sub]},
                           f.as_of))
    # order flow (tape)
    o = s["orderflow"]
    level_pts = (w["orderflow"] - o["recent_points"]) * _lin(win.ofi, o["ofi_lo"], o["ofi_hi"])
    recent_ok = f.recent.ofi is not None and f.recent.ofi >= o["recent_min"]
    comps.append(Component("orderflow", level_pts + (o["recent_points"] if recent_ok else 0.0), w["orderflow"],
                           {"ofi30": win.ofi, "ofi_recent": f.recent.ofi, "recent_ok": recent_ok}, f.as_of))
    # impact efficiency (Stage-1 page feature)
    e = s["efficiency"]
    eff_pct = (s1 or {}).get("eff_5m_pct")
    comps.append(Component("efficiency", w["efficiency"] * _lin(eff_pct, e["pct_lo"], e["pct_hi"]), w["efficiency"],
                           {"eff_5m_pct": eff_pct, "eff_5m": (s1 or {}).get("eff_5m")}, s1_ts))
    # structure (tape)
    st = s["structure"]
    pts = 0.0
    if f.price_vs_avwap_pct is not None and f.price_vs_avwap_pct >= 0:
        pts += st["avwap_points"]
    if f.clv_ge_half_2of3:
        pts += st["clv_points"]
    if f.higher_lows_2of3:
        pts += st["higher_lows_points"]
    comps.append(Component("structure", min(w["structure"], pts), w["structure"],
                           {"price_vs_avwap_pct": f.price_vs_avwap_pct, "clv_2of3": f.clv_ge_half_2of3,
                            "higher_lows_2of3": f.higher_lows_2of3, "bars": f.bars_n}, f.as_of))
    # holder growth (Stage-1 feature, soft)
    h = s["holder_growth"]
    hg = (s1 or {}).get("holder_growth_pct")
    comps.append(Component("holder_growth", w["holder_growth"] * _lin(hg, h["pct_lo"], h["pct_hi"]), w["holder_growth"],
                           {"holder_growth_pct": hg}, s1_ts))
    # safety bonus (T10 supplies; 0 until then)
    comps.append(Component("safety", max(0.0, min(w["safety"], safety_bonus)), w["safety"], {"bonus": safety_bonus}, None))
    return comps


def decide(chain: str, address: str, f: TapeFeatures, wash: WashReport, cfg: dict[str, Any] | None,
           eval_ts: int, s1_features: dict[str, Any] | None = None, s1_ts: int | None = None,
           fallback_anchor_ts: int | None = None, safety_bonus: float = 0.0,
           safety_verdict: str | None = None, safety_reasons: list[str] | None = None,
           safety_flags: list[str] | None = None) -> Decision:
    """safety_verdict: SAFE | UNSAFE | UNKNOWN | None. UNSAFE -> hard veto 'SAFETY:<reasons>';
    UNKNOWN -> tier capped at IGNITION (never CONFIRMED on unverified safety) + soft flag.
    safety_flags in CAP_FLAGS (bundle / sniper / risky token-2022 extensions) also cap at IGNITION (SPEC s6)."""
    s = _settings(cfg)
    comps = score_components(f, s1_features, s1_ts, s, safety_bonus)
    score = round(sum(c.points for c in comps), 2)
    hard, soft = list(wash.hard_vetoes), list(wash.soft_flags)
    cap = safety_verdict == "UNKNOWN"
    if safety_verdict == "UNSAFE":
        hard.append("SAFETY:" + "+".join(safety_reasons or ["unsafe"]))
    elif safety_verdict == "UNKNOWN":
        soft.append("SAFETY_UNKNOWN")
    for flag in safety_flags or []:
        if flag in CAP_FLAGS:
            soft.append(f"SAFETY:{flag}")
            cap = True
    if f.anchor_ts is not None:
        anchor_ts, source = f.anchor_ts, "tape"
    elif fallback_anchor_ts is not None:
        anchor_ts, source = int(fallback_anchor_ts), "stage1"
    else:
        anchor_ts, source = None, "none"
    ref_ts = f.as_of if f.as_of is not None else eval_ts
    since = (ref_ts - anchor_ts) if anchor_ts is not None else None
    lo, hi = s["eligible_after_anchor_s"]
    eligible = since is not None and int(lo) <= since <= int(hi)
    if hard:
        tier = "VETO"
    elif score >= float(s["tier_confirmed_score"]) and not cap:
        tier = "CONFIRMED"
    elif score >= float(s["tier_ignition_score"]):
        tier = "IGNITION"
    else:
        tier = "WATCH"
    alertable = eligible and tier in ("IGNITION", "CONFIRMED")
    return Decision(chain=chain, address=address, eval_ts=eval_ts, as_of=f.as_of, anchor_ts=anchor_ts,
                    anchor_source=source, since_anchor_s=since, score=score, tier=tier, eligible=eligible,
                    alertable=alertable, hard_vetoes=hard, soft_flags=soft, components=comps)


def persist_decision(conn: sqlite3.Connection, d: Decision, tape_features_id: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO decisions(chain, address, eval_ts, as_of, anchor_ts, anchor_source, since_anchor_s, score, tier, "
        "eligible, alertable, hard_vetoes, soft_flags, components_json, tape_features_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (d.chain, d.address, d.eval_ts, d.as_of, d.anchor_ts, d.anchor_source, d.since_anchor_s, d.score, d.tier,
         int(d.eligible), int(d.alertable), ",".join(d.hard_vetoes) or None, ",".join(d.soft_flags) or None,
         json.dumps([asdict(c) for c in d.components], separators=(",", ":"), default=str), tape_features_id))
    return int(cur.lastrowid)


def latest_stage1_features(conn: sqlite3.Connection, chain: str, address: str) -> tuple[dict[str, Any] | None, int | None]:
    r = conn.execute("SELECT ts, features_json FROM nominations WHERE chain=? AND address=? AND tier='WATCH' "
                     "ORDER BY ts DESC LIMIT 1", (chain, address)).fetchone()
    if r is None:
        return None, None
    try:
        return json.loads(r["features_json"]), int(r["ts"])
    except (TypeError, ValueError):
        return None, int(r["ts"])
