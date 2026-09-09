"""tune.py (T14) - evidence for the tuning pass at Milestone C. Analysis only; changes nothing.

Dataset: every event (nomination WATCH, CONTROL sample, or sent ALERT) joined to its
outcome labels at +5/+15/+30/+60 min -> ret_h, mfe_h, mae_h, liq_ratio_h, rug.
  win  = mfe at `win_horizon_min` >= win_threshold_pct   (default +30% within 15 min)
  rug  = liquidity fell to <= rug_liq_ratio of t0 at any horizon (default 0.4)

Outputs (markdown):
  1. group summary: n, win rate, big-win rate (+50%/30m), rug rate, median ret per horizon
  2. treatment vs control lift per horizon
  3. per-feature lift: within the treatment group, win rate when the feature is in its
     top half vs bottom half; ranked by lift = P(win | top half) / P(win | group).
     Controls carry the same Stage-1 feature vector, so a feature that separates winners
     from losers AND treatment from control is a real gate.
  4. MFE / MAE percentiles per horizon (where the stop and target belong)
  5. time-to-peak (horizon at which MFE is maximal) distribution
  6. expectancy under the fixed rule - APPROXIMATED at horizon granularity:
     enter at t0, stop if MAE_h <= -stop_pct (loss = -stop), else time-stop exit at
     ret_h (rule A: h=15) or trailing exit = max(ret_h, mfe_h*(1-trail)) (rule B: h=30).
     T15's replay evaluates the exact intrabar path; this is the coarse version.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

DEFAULTS: dict[str, Any] = {
    "win_horizon_min": 15,
    "win_threshold_pct": 30.0,
    "big_win_horizon_min": 30,
    "big_win_threshold_pct": 50.0,
    "rug_liq_ratio": 0.4,
    "stop_pct": 20.0,
    "trail_pct": 30.0,
    "min_group_n": 8,
    "features": ["rvol_5m", "rvol_1m", "eff_5m_pct", "eff_5m", "cohort_z", "holder_growth_pct", "turnover_5m",
                 "rvol_dt", "d_tr_1h", "score", "ofi30", "ofi_recent", "buyers30", "new_wallet_share", "wash_score",
                 "since_anchor_s"],
}


def _settings(cfg: dict[str, Any] | None) -> dict[str, Any]:
    s = dict(DEFAULTS)
    for k, v in (cfg or {}).items():
        if not k.startswith("_"):
            s[k] = v
    return s


@dataclass
class Event:
    kind: str                      # nomination | control | alert
    ref_id: int
    chain: str
    address: str
    t0_ts: int
    t0_price: float | None
    t0_liq: float | None
    features: dict[str, Any] = field(default_factory=dict)
    ret: dict[int, float] = field(default_factory=dict)      # horizon -> close return %
    mfe: dict[int, float] = field(default_factory=dict)
    mae: dict[int, float] = field(default_factory=dict)
    liq_ratio: dict[int, float] = field(default_factory=dict)
    tier: str | None = None

    def complete_at(self, h: int) -> bool:
        return h in self.ret


def _num(v: Any) -> float | None:
    try:
        return None if v is None or isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None


def load_events(conn: sqlite3.Connection, since_ts: int = 0, kinds: tuple[str, ...] = ("nomination", "control", "alert")) -> list[Event]:
    events: dict[tuple[str, int], Event] = {}
    # nominations + controls carry Stage-1 features
    for r in conn.execute("SELECT id, chain, address, ts, tier, price, liquidity, features_json FROM nominations WHERE ts>=?",
                          (since_ts,)):
        kind = "control" if r["tier"] == "CONTROL" else "nomination"
        if kind not in kinds:
            continue
        try:
            feats = json.loads(r["features_json"] or "{}")
        except json.JSONDecodeError:
            feats = {}
        events[(kind, int(r["id"]))] = Event(kind, int(r["id"]), r["chain"], r["address"], int(r["ts"]), _num(r["price"]),
                                              _num(r["liquidity"]), feats, tier=r["tier"])
    if "alert" in kinds:
        for r in conn.execute("SELECT a.id, a.chain, a.address, a.ts, a.tier, a.score, a.price, a.liquidity FROM alerts a "
                              "WHERE a.ts>=? AND a.status='sent'", (since_ts,)):
            ev = Event("alert", int(r["id"]), r["chain"], r["address"], int(r["ts"]), _num(r["price"]), _num(r["liquidity"]),
                       {"score": _num(r["score"])}, tier=r["tier"])
            # attach the decision that triggered it (score components, since_anchor) and its tape snapshot
            d = conn.execute("SELECT d.score, d.since_anchor_s, d.tape_features_id FROM decisions d WHERE d.chain=? AND d.address=? "
                             "AND d.alerted_ts=? ORDER BY d.id DESC LIMIT 1", (r["chain"], r["address"], int(r["ts"]))).fetchone()
            if d is not None:
                ev.features["since_anchor_s"] = _num(d["since_anchor_s"])
                if d["tape_features_id"]:
                    tf = conn.execute("SELECT ofi30, ofi_recent, buyers30, new_wallet_share, wash_score FROM tape_features WHERE id=?",
                                      (d["tape_features_id"],)).fetchone()
                    if tf is not None:
                        ev.features.update({k: _num(tf[k]) for k in ("ofi30", "ofi_recent", "buyers30", "new_wallet_share", "wash_score")})
            s1 = conn.execute("SELECT features_json FROM nominations WHERE chain=? AND address=? AND tier='WATCH' AND ts<=? "
                              "ORDER BY ts DESC LIMIT 1", (r["chain"], r["address"], int(r["ts"]))).fetchone()
            if s1 is not None:
                try:
                    base = json.loads(s1["features_json"] or "{}")
                    for k, v in base.items():
                        ev.features.setdefault(k, v)
                except json.JSONDecodeError:
                    pass
            events[("alert", int(r["id"]))] = ev
    # labels
    for r in conn.execute("SELECT ref_kind, ref_id, horizon_min, price, high, low, liquidity, t0_price, t0_liq, status FROM labels "
                          "WHERE t0_ts>=? AND status='done'", (since_ts,)):
        ev = events.get((r["ref_kind"], int(r["ref_id"])))
        if ev is None:
            continue
        p0 = ev.t0_price or _num(r["t0_price"])
        if not p0 or p0 <= 0:
            continue
        h = int(r["horizon_min"])
        if r["price"] is not None:
            ev.ret[h] = (float(r["price"]) / p0 - 1) * 100
        if r["high"] is not None:
            ev.mfe[h] = (float(r["high"]) / p0 - 1) * 100
        if r["low"] is not None:
            ev.mae[h] = (float(r["low"]) / p0 - 1) * 100
        l0 = ev.t0_liq or _num(r["t0_liq"])
        if r["liquidity"] is not None and l0:
            ev.liq_ratio[h] = float(r["liquidity"]) / l0
    return list(events.values())


# ---- metrics ---------------------------------------------------------------------------------

def is_win(ev: Event, s: dict[str, Any]) -> bool | None:
    h, thr = int(s["win_horizon_min"]), float(s["win_threshold_pct"])
    v = ev.mfe.get(h)
    if v is None:                       # controls have no path: fall back to close return
        v = ev.ret.get(h)
    return None if v is None else v >= thr


def is_big_win(ev: Event, s: dict[str, Any]) -> bool | None:
    h, thr = int(s["big_win_horizon_min"]), float(s["big_win_threshold_pct"])
    v = ev.mfe.get(h, ev.ret.get(h))
    return None if v is None else v >= thr


def is_rug(ev: Event, s: dict[str, Any]) -> bool | None:
    if not ev.liq_ratio:
        return None
    return min(ev.liq_ratio.values()) <= float(s["rug_liq_ratio"])


def _rate(flags: list[bool | None]) -> tuple[float | None, int]:
    vals = [f for f in flags if f is not None]
    return (sum(vals) / len(vals) if vals else None), len(vals)


def _pcts(values: list[float], ps=(10, 25, 50, 75, 90)) -> dict[int, float]:
    if not values:
        return {}
    v = sorted(values)
    return {p: v[min(len(v) - 1, int(len(v) * p / 100))] for p in ps}


@dataclass
class FeatureLift:
    name: str
    n: int
    split: float                 # median
    win_top: float | None
    win_bottom: float | None
    lift: float | None


def feature_lifts(events: list[Event], s: dict[str, Any]) -> list[FeatureLift]:
    out: list[FeatureLift] = []
    base_flags = [is_win(e, s) for e in events]
    base_rate, n_base = _rate(base_flags)
    if not base_rate or n_base < int(s["min_group_n"]):
        return out
    for name in s["features"]:
        pairs = [(_num(e.features.get(name)), is_win(e, s)) for e in events]
        pairs = [(x, w) for x, w in pairs if x is not None and w is not None]
        if len(pairs) < int(s["min_group_n"]):
            continue
        med = statistics.median(x for x, _ in pairs)
        top = [w for x, w in pairs if x > med]
        bottom = [w for x, w in pairs if x <= med]
        if len(top) < 3 or len(bottom) < 3:
            continue
        wt, wb = sum(top) / len(top), sum(bottom) / len(bottom)
        out.append(FeatureLift(name, len(pairs), med, wt, wb, (wt / base_rate) if base_rate else None))
    out.sort(key=lambda f: -(f.lift or 0))
    return out


def expectancy(events: list[Event], s: dict[str, Any]) -> dict[str, Any]:
    stop, trail = float(s["stop_pct"]), float(s["trail_pct"]) / 100
    a: list[float] = []
    b: list[float] = []
    for e in events:
        if 15 in e.ret and 15 in e.mae:
            a.append(-stop if e.mae[15] <= -stop else e.ret[15])
        if 30 in e.ret and 30 in e.mae and 30 in e.mfe:
            if e.mae[30] <= -stop:
                b.append(-stop)
            else:
                b.append(max(e.ret[30], e.mfe[30] * (1 - trail)))
    def summ(x: list[float]) -> dict[str, Any]:
        if not x:
            return {"n": 0}
        wins = [v for v in x if v > 0]
        return {"n": len(x), "mean_pct": statistics.fmean(x), "median_pct": statistics.median(x),
                "win_rate": len(wins) / len(x), "avg_win_pct": statistics.fmean(wins) if wins else 0.0,
                "avg_loss_pct": statistics.fmean([v for v in x if v <= 0]) if len(wins) < len(x) else 0.0}
    return {"rule_A_stop_then_time_stop_15m": summ(a), "rule_B_stop_then_trail_30m": summ(b)}


def time_to_peak(events: list[Event]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for e in events:
        if not e.mfe:
            continue
        h = max(e.mfe, key=lambda k: (e.mfe[k], -k))
        counts[h] = counts.get(h, 0) + 1
    return dict(sorted(counts.items()))


# ---- report ------------------------------------------------------------------------------------

def _f(v: Any, fmt: str = "{:.1f}") -> str:
    return "-" if v is None else fmt.format(v)


def build_report(conn: sqlite3.Connection, cfg: dict[str, Any] | None = None, since_ts: int = 0,
                 now: int | None = None) -> str:
    s = _settings(cfg)
    now = int(time.time()) if now is None else now
    events = load_events(conn, since_ts)
    groups = {k: [e for e in events if e.kind == k] for k in ("alert", "nomination", "control")}
    lines = [f"# tune report - {time.strftime('%Y-%m-%d %H:%M', time.gmtime(now))} UTC",
             f"win = MFE(+{s['win_horizon_min']}m) >= +{s['win_threshold_pct']:.0f}% (controls: close return) · "
             f"big win = +{s['big_win_threshold_pct']:.0f}% within {s['big_win_horizon_min']}m · rug = liquidity <= "
             f"{s['rug_liq_ratio']:.0%} of t0 at any horizon", ""]
    lines.append("## 1. Groups")
    lines.append("| group | n | labeled@15 | win rate | big win | rug rate | median ret 5/15/30/60 |")
    lines.append("|---|---|---|---|---|---|---|")
    for k, evs in groups.items():
        lab = [e for e in evs if e.complete_at(15)]
        wr, nw = _rate([is_win(e, s) for e in evs])
        bw, _ = _rate([is_big_win(e, s) for e in evs])
        rr, _ = _rate([is_rug(e, s) for e in evs])
        med = "/".join(_f(statistics.median([e.ret[h] for e in evs if h in e.ret]) if any(h in e.ret for e in evs) else None, "{:+.0f}")
                       for h in (5, 15, 30, 60))
        lines.append(f"| {k} | {len(evs)} | {len(lab)} | {_f(wr, '{:.0%}')} (n={nw}) | {_f(bw, '{:.0%}')} | {_f(rr, '{:.0%}')} | {med} |")
    lines.append("")
    lines.append("## 2. Treatment vs control (win rate lift)")
    ctrl_rate, nc = _rate([is_win(e, s) for e in groups["control"]])
    for k in ("alert", "nomination"):
        r, n = _rate([is_win(e, s) for e in groups[k]])
        lift = (r / ctrl_rate) if (r is not None and ctrl_rate) else None
        lines.append(f"- {k}: {_f(r, '{:.0%}')} (n={n}) vs control {_f(ctrl_rate, '{:.0%}')} (n={nc}) -> lift {_f(lift, '{:.2f}x')}")
    lines.append("")
    for k in ("alert", "nomination"):
        evs = groups[k]
        lifts = feature_lifts(evs, s)
        lines.append(f"## 3. Feature lift within {k}s (top half vs bottom half by median)")
        if not lifts:
            lines.append(f"_not enough labeled {k}s yet (need >= {s['min_group_n']})_")
        else:
            lines.append("| feature | n | median split | win top | win bottom | lift |")
            lines.append("|---|---|---|---|---|---|")
            for fl in lifts[:14]:
                lines.append(f"| {fl.name} | {fl.n} | {_f(fl.split, '{:.3g}')} | {_f(fl.win_top, '{:.0%}')} | "
                             f"{_f(fl.win_bottom, '{:.0%}')} | {_f(fl.lift, '{:.2f}x')} |")
        lines.append("")
    tre = groups["alert"] if len([e for e in groups["alert"] if e.mfe]) >= int(s["min_group_n"]) else groups["nomination"]
    label = "alerts" if tre is groups["alert"] else "nominations"
    lines.append(f"## 4. MFE / MAE percentiles ({label}, % from t0)")
    lines.append("| horizon | n | MFE p25 | MFE p50 | MFE p75 | MFE p90 | MAE p10 | MAE p25 | MAE p50 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for h in (5, 15, 30, 60):
        mf = _pcts([e.mfe[h] for e in tre if h in e.mfe])
        ma = _pcts([e.mae[h] for e in tre if h in e.mae])
        n = len([e for e in tre if h in e.mfe])
        lines.append(f"| +{h}m | {n} | {_f(mf.get(25), '{:+.0f}')} | {_f(mf.get(50), '{:+.0f}')} | {_f(mf.get(75), '{:+.0f}')} | "
                     f"{_f(mf.get(90), '{:+.0f}')} | {_f(ma.get(10), '{:+.0f}')} | {_f(ma.get(25), '{:+.0f}')} | {_f(ma.get(50), '{:+.0f}')} |")
    lines.append("")
    ttp = time_to_peak(tre)
    lines.append(f"## 5. Time to peak ({label}, horizon where MFE is maximal)")
    lines.append(", ".join(f"+{h}m: {n}" for h, n in ttp.items()) if ttp else "_no path data yet_")
    lines.append("")
    ex = expectancy(tre, s)
    lines.append(f"## 6. Expectancy under the fixed rule ({label}; stop {s['stop_pct']:.0f}%, trail {s['trail_pct']:.0f}%) - horizon-granular approximation")
    for name, v in ex.items():
        if v.get("n"):
            lines.append(f"- {name}: n={v['n']} mean {v['mean_pct']:+.1f}% median {v['median_pct']:+.1f}% win rate {v['win_rate']:.0%} "
                         f"avg win {v['avg_win_pct']:+.1f}% avg loss {v['avg_loss_pct']:+.1f}%")
        else:
            lines.append(f"- {name}: n=0")
    lines.append("")
    lines.append("_Read with care: n is small until Milestone B completes; lift on < 30 events is noise. "
                 "T15's replay evaluates exact paths; this report is the coarse view._")
    return "\n".join(lines)
