"""Replay backtester (T15) - the reproducibility gate and the exact-path evaluator.

1. replay_decisions: for every live decision, rebuild the tape from `trades`
   (last ring_size trades with ts <= the decision's as_of), recompute features,
   wash and the decision from stored inputs (Stage-1 features as of eval time,
   safety verdict/bonus/flags as recorded on the decision) and compare:
     features  n, ofi30, buyers, anchor_ts, avwap, price_vs_anchor (tolerance 1e-6)
     wash      wash_score, hard vetoes
     decision  score, tier, eligible, since_anchor
   A mismatch means the live path used state that is not in the database
   (ring truncation, later backfill, enrichment cache drift) - it is reported,
   never hidden. Stage 1 replay lives in scanner/replay.py.
   Knowledge time (schema v8): trades carry ingested_ts and decisions carry
   config_hash. When present, the tape is filtered to trades known by eval_ts
   and the decision is re-derived under its own config version; those rows
   form the "exactly reproducible subset". Rows without either are legacy.

2. evaluate_paths: exact intrabar outcomes on the 1-minute candles the labeler
   recorded (raw recorder, endpoint ohlcv_v3), under the fixed rules:
     stop     first candle whose low <= entry*(1-stop)           -> -stop
     rule A   else close at +15 min (time stop)
     rule B   else trail: exit when a candle low <= peak*(1-trail), or close at +30
   Conservative: a candle that hits both stop and a new high counts as the stop.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .features import compute as compute_features
from .scoring import decide
from .tape import Trade
from .wash import evaluate as evaluate_wash

TOL = 1e-6


def _close(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= TOL * max(1.0, abs(a), abs(b))


@dataclass
class DecisionReplay:
    decision_id: int
    chain: str
    address: str
    eval_ts: int
    features_ok: bool
    wash_ok: bool
    decision_ok: bool
    diffs: list[str] = field(default_factory=list)


@dataclass
class ReplayReport:
    n: int = 0
    features_ok: int = 0
    wash_ok: int = 0
    decision_ok: int = 0
    skipped: int = 0
    runtime_s: float = 0.0
    samples: list[DecisionReplay] = field(default_factory=list)
    # reproducible subset: decisions with a config version AND knowledge-time trades
    exact_n: int = 0
    exact_decision_ok: int = 0
    legacy_no_config: int = 0
    legacy_no_knowledge: int = 0

    @property
    def decision_match_rate(self) -> float:
        return self.decision_ok / self.n if self.n else 0.0

    @property
    def exact_match_rate(self) -> float:
        return self.exact_decision_ok / self.exact_n if self.exact_n else 0.0


def load_tape(conn: sqlite3.Connection, chain: str, address: str, as_of: int, ring_size: int,
              known_by_ts: int | None = None) -> tuple[list[Trade], bool]:
    """Trades with ts <= as_of. When the token's rows carry ingested_ts, only trades KNOWN by known_by_ts
    are used (knowledge time, not event time) -> (trades, knowledge_filtered)."""
    has_knowledge = known_by_ts is not None and conn.execute(
        "SELECT 1 FROM trades WHERE chain=? AND address=? AND ingested_ts IS NOT NULL LIMIT 1", (chain, address)).fetchone() is not None
    if has_knowledge:
        rows = conn.execute(
            "SELECT sig, ts, side, wallet, usd, price, amount, source FROM trades WHERE chain=? AND address=? AND ts<=? "
            "AND (ingested_ts IS NULL OR ingested_ts<=?) ORDER BY ts DESC, sig DESC LIMIT ?",
            (chain, address, as_of, known_by_ts, ring_size)).fetchall()
    else:
        rows = conn.execute(
            "SELECT sig, ts, side, wallet, usd, price, amount, source FROM trades WHERE chain=? AND address=? AND ts<=? "
            "ORDER BY ts DESC, sig DESC LIMIT ?", (chain, address, as_of, ring_size)).fetchall()
    trades = [Trade(chain, address, r["sig"], int(r["ts"]), r["side"], r["wallet"], r["usd"], r["price"], r["amount"],
                    r["source"], tx_hash=str(r["sig"]).split(":")[0]) for r in rows]
    return sorted(trades, key=lambda t: (t.ts, t.sig)), has_knowledge


def stage1_features_as_of(conn: sqlite3.Connection, chain: str, address: str, eval_ts: int) -> tuple[dict[str, Any] | None, int | None]:
    r = conn.execute("SELECT ts, features_json FROM nominations WHERE chain=? AND address=? AND tier='WATCH' AND ts<=? "
                     "ORDER BY ts DESC LIMIT 1", (chain, address, eval_ts)).fetchone()
    if r is None:
        return None, None
    try:
        return json.loads(r["features_json"]), int(r["ts"])
    except (TypeError, ValueError):
        return None, int(r["ts"])


def recorded_safety(decision: sqlite3.Row, components: list[dict[str, Any]]) -> tuple[str | None, list[str], list[str], float]:
    """Reconstruct the safety inputs the live decision saw from what it recorded."""
    hard = [h for h in (decision["hard_vetoes"] or "").split(",") if h]
    soft = [f for f in (decision["soft_flags"] or "").split(",") if f]
    bonus = next((float(c.get("points", 0.0)) for c in components if c.get("name") == "safety"), 0.0)
    unsafe = [h for h in hard if h.startswith("SAFETY:")]
    if unsafe:
        return "UNSAFE", [unsafe[0][len("SAFETY:"):]], [], bonus
    if "SAFETY_UNKNOWN" in soft:
        return "UNKNOWN", [], [f[len("SAFETY:"):] for f in soft if f.startswith("SAFETY:")], bonus
    flags = [f[len("SAFETY:"):] for f in soft if f.startswith("SAFETY:")]
    return ("SAFE" if (bonus > 0 or flags) else None), [], flags, bonus


def replay_decisions(conn: sqlite3.Connection, cfg: dict[str, Any], since_ts: int = 0, limit: int | None = None,
                     sample_mismatches: int = 12) -> ReplayReport:
    from .db import config_by_hash
    t0 = time.monotonic()
    rep = ReplayReport()
    q = ("SELECT d.*, tf.features_json AS tf_features, tf.wash_json AS tf_wash FROM decisions d "
         "LEFT JOIN tape_features tf ON tf.id = d.tape_features_id WHERE d.eval_ts>=? ORDER BY d.id")
    rows = conn.execute(q, (since_ts,)).fetchall()
    if limit:
        rows = rows[-limit:]
    cfg_cache: dict[str, dict[str, Any]] = {}
    for d in rows:
        if not d["tf_features"] or d["as_of"] is None:
            rep.skipped += 1
            continue
        rep.n += 1
        # the tunables that produced THIS decision (config version), else the current ones
        h = d["config_hash"] if "config_hash" in d.keys() else None
        if h and h not in cfg_cache:
            cfg_cache[h] = config_by_hash(conn, h) or cfg
        dcfg = cfg_cache.get(h, cfg) if h else cfg
        if not h:
            rep.legacy_no_config += 1
        ring = int(dcfg.get("tape", {}).get("ring_size", 600))
        fcfg = dcfg.get("stage2", {}).get("features", {})
        wcfg = dcfg.get("stage2", {}).get("wash", {})
        scfg = dcfg.get("scoring", {})
        live_f = json.loads(d["tf_features"])
        live_w = json.loads(d["tf_wash"]) if d["tf_wash"] else {}
        comps = json.loads(d["components_json"] or "[]")
        trades, knowledge = load_tape(conn, d["chain"], d["address"], int(d["as_of"]), ring, known_by_ts=int(d["eval_ts"]))
        if not knowledge:
            rep.legacy_no_knowledge += 1
        exact = bool(h) and knowledge
        f = compute_features(trades, fcfg, as_of=int(d["as_of"]))
        diffs: list[str] = []
        checks = [("n", f.n, live_f.get("n")), ("ofi30", f.ofi30.ofi, (live_f.get("ofi30") or {}).get("ofi")),
                  ("buyers30", float(f.ofi30.buyers), float((live_f.get("ofi30") or {}).get("buyers") or 0)),
                  ("anchor_ts", f.anchor_ts, live_f.get("anchor_ts")), ("avwap", f.avwap, live_f.get("avwap")),
                  ("price_vs_anchor_pct", f.price_vs_anchor_pct, live_f.get("price_vs_anchor_pct"))]
        for name, a, b in checks:
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                if not _close(float(a), float(b)):
                    diffs.append(f"feature {name}: replay {a} vs live {b}")
            elif a != b:
                diffs.append(f"feature {name}: replay {a} vs live {b}")
        features_ok = not diffs
        # wash: recompute with the enrichment currently cached (may have drifted) - compare score + hard vetoes
        liq_r = conn.execute("SELECT liquidity FROM scan_rows WHERE chain=? AND address=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                             (d["chain"], d["address"], int(d["eval_ts"]))).fetchone()
        liq = float(liq_r["liquidity"]) if liq_r and liq_r["liquidity"] is not None else None
        w = evaluate_wash(trades, f, wcfg, liquidity=liq, as_of=int(d["as_of"]))
        wash_ok = _close(w.wash_score, live_w.get("wash_score"))
        live_hard = set(live_w.get("hard_vetoes") or [])
        if set(w.hard_vetoes) - {"DISTRIBUTION"} != live_hard - {"DISTRIBUTION"}:   # DISTRIBUTION needs holdings (network)
            wash_ok = False
        if not wash_ok:
            diffs.append(f"wash: replay {w.wash_score} {w.hard_vetoes} vs live {live_w.get('wash_score')} {sorted(live_hard)}")
        # decision: stored Stage-1 features as of eval time + recorded safety inputs
        s1, s1_ts = stage1_features_as_of(conn, d["chain"], d["address"], int(d["eval_ts"]))
        verdict, reasons, flags, bonus = recorded_safety(d, comps)
        # use the LIVE wash for vetoes so a network-dependent DISTRIBUTION veto does not mask the scoring check
        class _W:
            hard_vetoes = [h for h in live_hard if not h.startswith("SAFETY:")]
            soft_flags = [x for x in (d["soft_flags"] or "").split(",") if x and not x.startswith("SAFETY")]
        fallback = d["anchor_ts"] if d["anchor_source"] == "stage1" else None
        dec = decide(d["chain"], d["address"], f, _W(), scfg, int(d["eval_ts"]), s1_features=s1, s1_ts=s1_ts,
                     fallback_anchor_ts=fallback, safety_bonus=bonus, safety_verdict=verdict, safety_reasons=reasons,
                     safety_flags=flags)
        dec_diffs = []
        if not _close(dec.score, float(d["score"])):
            dec_diffs.append(f"score {dec.score} vs {d['score']}")
        if dec.tier != d["tier"]:
            dec_diffs.append(f"tier {dec.tier} vs {d['tier']}")
        if bool(dec.eligible) != bool(d["eligible"]):
            dec_diffs.append(f"eligible {dec.eligible} vs {bool(d['eligible'])}")
        if dec.since_anchor_s != d["since_anchor_s"]:
            dec_diffs.append(f"since_anchor {dec.since_anchor_s} vs {d['since_anchor_s']}")
        decision_ok = not dec_diffs
        diffs.extend("decision " + x for x in dec_diffs)
        rep.features_ok += features_ok
        rep.wash_ok += wash_ok
        rep.decision_ok += decision_ok
        if exact:
            rep.exact_n += 1
            rep.exact_decision_ok += decision_ok
        if diffs and len(rep.samples) < sample_mismatches:
            rep.samples.append(DecisionReplay(int(d["id"]), d["chain"], d["address"], int(d["eval_ts"]), features_ok, wash_ok,
                                              decision_ok, diffs))
    rep.runtime_s = time.monotonic() - t0
    return rep


# ---- exact-path evaluation on recorded 1m candles ----------------------------------------------

def load_recorded_paths(raw_dir: Path, since_ts: int = 0) -> dict[tuple[str, str, int], list[dict[str, Any]]]:
    """{(chain, address, time_from): candles} from recorded ohlcv_v3 responses (the labeler's path calls)."""
    out: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for f in sorted(Path(raw_dir).glob("*.jsonl.gz")):
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("endpoint") != "ohlcv_v3" or r.get("status") != 200 or r.get("ts", 0) < since_ts:
                    continue
                p = r.get("params") or {}
                items = ((r.get("payload") or {}).get("data") or {}).get("items") or []
                if p.get("type") == "1m" and items and "address" in p and "time_from" in p:
                    key = (r.get("chain") or "", str(p["address"]), int(p["time_from"]))
                    if key not in out or len(items) > len(out[key]):
                        out[key] = items
    return out


@dataclass
class PathOutcome:
    kind: str
    ref_id: int
    chain: str
    address: str
    rule_a_pct: float
    rule_b_pct: float
    stop_hit: bool
    peak_pct: float
    minutes_to_peak: int


def run_rules(candles: list[dict[str, Any]], entry: float, stop_pct: float, trail_pct: float,
              time_stop_min: int = 15, trail_horizon_min: int = 30) -> tuple[float, float, bool, float, int]:
    """-> (rule A %, rule B %, stop_hit, peak %, minutes_to_peak). Conservative on ambiguous candles."""
    cs = sorted((c for c in candles if c.get("unix_time") is not None), key=lambda c: c["unix_time"])
    if not cs or entry <= 0:
        return 0.0, 0.0, False, 0.0, 0
    stop_lvl = entry * (1 - stop_pct / 100)
    t0 = int(cs[0]["unix_time"])
    peak = entry
    peak_min = 0
    a_done = b_done = False
    a = b = 0.0
    stop_hit = False
    for c in cs:
        m = (int(c["unix_time"]) - t0) // 60
        lo, hi, close = float(c["l"]), float(c["h"]), float(c["c"])
        if hi > peak:
            peak, peak_min = hi, m
        if not a_done:
            if lo <= stop_lvl:
                a, a_done, stop_hit = -stop_pct, True, True
            elif m >= time_stop_min:
                a, a_done = (close / entry - 1) * 100, True
        if not b_done:
            if lo <= stop_lvl:
                b, b_done, stop_hit = -stop_pct, True, True
            elif peak > entry and lo <= peak * (1 - trail_pct / 100):
                b, b_done = (peak * (1 - trail_pct / 100) / entry - 1) * 100, True
            elif m >= trail_horizon_min:
                b, b_done = (close / entry - 1) * 100, True
        if a_done and b_done:
            break
    if not a_done:
        a = (float(cs[-1]["c"]) / entry - 1) * 100
    if not b_done:
        b = (float(cs[-1]["c"]) / entry - 1) * 100
    return a, b, stop_hit, (peak / entry - 1) * 100, peak_min


def evaluate_paths(conn: sqlite3.Connection, raw_dir: Path, cfg: dict[str, Any], since_ts: int = 0) -> list[PathOutcome]:
    tcfg = cfg.get("tune", {})
    stop, trail = float(tcfg.get("stop_pct", 20.0)), float(tcfg.get("trail_pct", 30.0))
    paths = load_recorded_paths(raw_dir, since_ts)
    out: list[PathOutcome] = []
    rows = conn.execute("SELECT DISTINCT ref_kind, ref_id, chain, address, t0_ts, t0_price FROM labels "
                        "WHERE ref_kind IN ('nomination','alert') AND path_status='done' AND t0_ts>=?", (since_ts,)).fetchall()
    for r in rows:
        candles = paths.get((r["chain"], r["address"], int(r["t0_ts"])))
        if not candles or not r["t0_price"]:
            continue
        a, b, hit, peak, pm = run_rules(candles, float(r["t0_price"]), stop, trail)
        out.append(PathOutcome(r["ref_kind"], int(r["ref_id"]), r["chain"], r["address"], a, b, hit, peak, pm))
    return out


def format_report(rep: ReplayReport, paths: list[PathOutcome]) -> str:
    lines = ["# backtest report", "",
             "## Reproducibility (live decisions re-derived from the database)",
             f"decisions={rep.n} skipped(no snapshot)={rep.skipped} runtime={rep.runtime_s:.1f}s",
             f"features match {rep.features_ok}/{rep.n} · wash match {rep.wash_ok}/{rep.n} · "
             f"decision match {rep.decision_ok}/{rep.n} ({rep.decision_match_rate:.1%})",
             f"exactly reproducible subset (config version + knowledge-time trades): {rep.exact_decision_ok}/{rep.exact_n} "
             f"({rep.exact_match_rate:.1%}) · legacy without config version: {rep.legacy_no_config} · "
             f"legacy without knowledge time: {rep.legacy_no_knowledge}"]
    for s in rep.samples:
        lines.append(f"- #{s.decision_id} {s.chain} {s.address[:8]} @{s.eval_ts}: " + "; ".join(s.diffs)[:300])
    lines += ["", "## Exact-path outcomes on recorded 1m candles (fixed rules)"]
    for kind in ("alert", "nomination"):
        ps = [p for p in paths if p.kind == kind]
        if not ps:
            lines.append(f"- {kind}: no recorded paths yet")
            continue
        import statistics as st
        a = [p.rule_a_pct for p in ps]
        b = [p.rule_b_pct for p in ps]
        lines.append(f"- {kind}: n={len(ps)} stop hit {sum(p.stop_hit for p in ps) / len(ps):.0%} · "
                     f"rule A mean {st.fmean(a):+.1f}% median {st.median(a):+.1f}% win {sum(x > 0 for x in a) / len(a):.0%} · "
                     f"rule B mean {st.fmean(b):+.1f}% median {st.median(b):+.1f}% win {sum(x > 0 for x in b) / len(b):.0%} · "
                     f"peak median {st.median([p.peak_pct for p in ps]):+.0f}% at {st.median([p.minutes_to_peak for p in ps]):.0f} min")
    return "\n".join(lines)
