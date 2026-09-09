"""Main loop: one asyncio task per enabled chain, each running Stage 0 on its
own interval. Later tasks plug Stage 1/2/3 into `on_cycle`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .birdeye import BirdeyeClient
from .config import ChainConfig, Config
from .db import open_db
from .ledger import CULedger
from .plans import cu_cost, daily_cu_budget
from .recorder import RawRecorder
from .labeler import Labeler
from .stage0 import CycleResult, Stage0Scanner
from .stage1 import Stage1
from .candidates import CandidateManager
from .enrichment import Enricher
from .features import compute as compute_features
from .safety import SafetyChecker
from .scoring import decide, latest_stage1_features, persist_decision
from .tape import TapePoller, TapeStore, persist_features
from .wash import evaluate as evaluate_wash

log = logging.getLogger("runner")


@dataclass
class ChainStats:
    cycles: int = 0
    ok: int = 0
    errors: int = 0
    skipped: int = 0
    fetched: int = 0
    persisted: int = 0
    violations: int = 0
    duplicates: int = 0
    cu: int = 0
    violation_samples: list = field(default_factory=list)
    s1_evaluated: int = 0
    s1_passed: int = 0
    s1_nominated: int = 0
    s1_cooldown: int = 0
    s1_fail_counts: dict = field(default_factory=dict)
    s1_nominees: list = field(default_factory=list)
    labels_created: int = 0
    controls_drawn: int = 0


@dataclass
class RunSummary:
    started_ts: float
    ended_ts: float
    stats: dict[str, ChainStats]
    cu_today_total: int
    projected_cu_per_day: int
    rh_rows_with_5m: int | None
    rh_rows_total: int | None
    label_ticks: int = 0
    labels_done: int = 0
    labels_failed: int = 0
    paths_done: int = 0
    label_counts: dict = field(default_factory=dict)
    tape_polls: int = 0
    tape_fetched: int = 0
    tape_new: int = 0
    tape_cu: int = 0
    tape_skipped_budget: int = 0
    tape_errors: int = 0
    candidate_stats: dict = field(default_factory=dict)
    decision_totals: dict = field(default_factory=dict)
    safety_totals: dict = field(default_factory=dict)


def _fmt(v: float | None, suffix: str = "") -> str:
    return "-" if v is None else f"{v:.2f}{suffix}"


def format_decision_line(d, symbol: str | None) -> str:
    comps = " ".join(f"{c.name[:4]}={c.points:.0f}/{c.max_points:.0f}" for c in d.components)
    since = f"{d.since_anchor_s}s" if d.since_anchor_s is not None else "-"
    return (f"decision {d.chain} {symbol or d.address[:8]}: score={d.score:.0f} [{comps}] tier={d.tier} "
            f"anchor={d.anchor_ts}({d.anchor_source}) since={since} eligible={d.eligible}"
            + (" *** ALERTABLE ***" if d.alertable else ""))


def projected_cu_per_day(cfg: Config) -> int:
    cu, _ = cu_cost("token_list_v3")
    return int(sum(cu * 86400 / ch.scan_interval_s for ch in cfg.enabled_chains))


async def run_loop(cfg: Config, duration_s: float | None = None, once: bool = False) -> RunSummary:
    conn = open_db(cfg.db_path)
    ledger = CULedger(conn)
    recorder = RawRecorder(cfg.raw_dir, enabled=cfg.recorder_enabled)
    birdeye_cfg = cfg.raw.get("birdeye", {})
    daily_cap = int(birdeye_cfg.get("daily_cu_cap") or daily_cu_budget(cfg.birdeye_plan))
    started = time.time()
    stats: dict[str, ChainStats] = {ch.name: ChainStats() for ch in cfg.enabled_chains}

    log.info("run start: plan=%s daily_cu_cap=%d cu_today=%d projected_cu/day=%d chains=%s",
             cfg.birdeye_plan, daily_cap, ledger.today_total(), projected_cu_per_day(cfg),
             [f"{c.name}@{c.scan_interval_s}s" for c in cfg.enabled_chains])

    async with BirdeyeClient(api_key=cfg.secret("BIRDEYE_API_KEY") or "", plan=cfg.birdeye_plan,
                             base_url=cfg.birdeye_base_url, recorder=recorder, ledger=ledger,
                             rate_safety=float(birdeye_cfg.get("rate_safety", 0.8))) as client:
        scanner = Stage0Scanner(conn, client, ledger, daily_cu_cap=daily_cap)
        stage1 = Stage1(conn, persist=True)
        labeler = Labeler(conn, client, ledger, cfg.raw.get("labeler", {}), cfg.birdeye_plan, daily_cap)
        chain_map = {ch.name: ch for ch in cfg.enabled_chains}
        label_totals = {"ticks": 0, "done": 0, "failed": 0, "paths": 0}
        tape_cfg = cfg.raw.get("tape", {})
        store = TapeStore(conn, ring_size=int(tape_cfg.get("ring_size", 600)))
        poller = TapePoller(client, store, ledger, tape_cfg, daily_cap)
        tape_totals = {"polls": 0, "fetched": 0, "new": 0, "cu": 0, "budget": 0, "errors": 0}
        stage2_cfg = cfg.raw.get("stage2", {})
        enricher = Enricher(conn, client, ledger, stage2_cfg.get("enrichment", {}), daily_cap)
        wash_cfg = stage2_cfg.get("wash", {})
        scoring_cfg = cfg.raw.get("scoring", {})
        decision_totals: dict[str, int] = {}
        safety = SafetyChecker(conn, client, ledger, cfg.raw.get("safety", {}), daily_cap,
                               {"solana": cfg.secret("SOLANA_RPC_URL"), "robinhood": cfg.secret("ROBINHOOD_RPC_URL")})
        safety_totals: dict[str, int] = {}

        def latest_liquidity(chain: str, address: str) -> float | None:
            r = conn.execute("SELECT liquidity FROM scan_rows WHERE chain=? AND address=? ORDER BY ts DESC LIMIT 1",
                             (chain, address)).fetchone()
            return float(r["liquidity"]) if r and r["liquidity"] is not None else None
        manager = CandidateManager(conn, ledger, cfg.raw.get("candidates", {}), daily_cap)
        if manager.active_set:
            log.info("candidates restored from DB: %d active", len(manager.active_set))

        def s1_rvol_of(ev) -> float | None:
            f = ev.features
            return f.rvol_5m if f.mode == "short" else f.rvol_dt

        def account(res: CycleResult) -> None:
            st = stats[res.chain]
            st.cycles += 1
            st.cu += res.cu
            if res.error:
                st.errors += 1
            elif res.skipped:
                st.skipped += 1
            else:
                st.ok += 1
                st.fetched += res.fetched
                st.persisted += res.persisted
                st.violations += res.violations
                st.duplicates += res.duplicates
                if res.violation_samples and len(st.violation_samples) < 5:
                    st.violation_samples.extend(res.violation_samples[:5 - len(st.violation_samples)])
            log.info("%s cycle %d sort=%s fetched=%d persisted=%d violations=%d dup=%d cu=%d %dms%s%s",
                     res.chain, res.cycle_id, res.sort_key, res.fetched, res.persisted, res.violations,
                     res.duplicates, res.cu, res.latency_ms,
                     f" SKIPPED({res.skipped})" if res.skipped else "",
                     f" ERROR({res.error})" if res.error else "")

        async def chain_loop(ch: ChainConfig) -> None:
            while True:
                t0 = time.monotonic()
                try:
                    res = await scanner.cycle(ch)
                    account(res)
                    if res.rows:
                        s1 = stage1.run_cycle(ch, res.rows, res.ts, res.cycle_id)
                        st = stats[ch.name]
                        st.s1_evaluated += s1.evaluated
                        st.s1_passed += s1.passed
                        st.s1_nominated += s1.nominated
                        st.s1_cooldown += s1.cooldown
                        for k, v in s1.fail_counts.items():
                            st.s1_fail_counts[k] = st.s1_fail_counts.get(k, 0) + v
                        st.s1_nominees.extend(s1.nominees)
                        log.info("%s cycle %d stage1: evaluated=%d passed=%d nominated=%d cooldown=%d%s",
                                 ch.name, res.cycle_id, s1.evaluated, s1.passed, s1.nominated, s1.cooldown,
                                 (" nominees=" + ", ".join(f"{s}" for s, _ in s1.nominees)) if s1.nominees else "")
                        # candidate lifecycle: nominations ENTER, active tokens on the page get a STAY check
                        by_addr = {ev.row.address: ev for ev in s1.evals}
                        outcomes: dict[str, int] = {}
                        for nom_id, row in s1.nominated_rows:
                            ev = by_addr.get(row.address)
                            o = manager.on_nomination(ch, row.address, row.symbol, s1_rvol_of(ev) if ev else None,
                                                      res.ts, anchor_ts=res.ts, anchor_price=row.price)
                            outcomes[o] = outcomes.get(o, 0) + 1
                        nominated_addrs = {row.address for _, row in s1.nominated_rows}
                        for c in manager.active(ch, res.ts):
                            ev = by_addr.get(c.address)
                            if ev is not None and c.address not in nominated_addrs:
                                manager.on_stage1_row(ch, c.address, s1_rvol_of(ev), res.ts)
                        if outcomes or manager.stats.events:
                            log.info("%s candidates: %s active=%d%s", ch.name, outcomes or "-",
                                     len(manager.active(ch, res.ts)),
                                     (" | " + "; ".join(manager.stats.events[-3:])) if manager.stats.events else "")
                            manager.stats.events.clear()
                        # immediate first contact: don't wait for the next tape tick (saves up to poll_interval_s)
                        fresh = [(ch, row.address) for _, row in s1.nominated_rows
                                 if (ch.name, row.address) in manager.active_set
                                 and manager.active_set[(ch.name, row.address)].polls == 0
                                 and store.get(ch.name, row.address, load_from_db=False).polls == 0]
                        if fresh and not manager.degraded(res.ts):
                            asyncio.create_task(poll_and_evaluate(fresh, "on-nomination"))
                        lb = labeler.on_cycle(ch, s1, res.ts, res.cycle_id)
                        st.labels_created += lb.labels_created
                        st.controls_drawn += lb.controls_drawn
                        if lb.labels_created:
                            log.info("%s cycle %d labeler: enqueued %d nominations + %d controls -> %d label rows",
                                     ch.name, res.cycle_id, lb.nominations_enqueued, lb.controls_drawn, lb.labels_created)
                except Exception:  # noqa: BLE001 - never let one chain kill the loop
                    log.exception("%s cycle crashed", ch.name)
                    stats[ch.name].cycles += 1
                    stats[ch.name].errors += 1
                if once:
                    return
                await asyncio.sleep(max(0.0, ch.scan_interval_s - (time.monotonic() - t0)))

        async def label_loop() -> None:
            while True:
                try:
                    ts = await labeler.tick(chain_map)
                    label_totals["ticks"] += 1
                    label_totals["done"] += ts.done_scan_row + ts.done_multi_price
                    label_totals["failed"] += ts.failed + ts.path_failed
                    label_totals["paths"] += ts.path_done
                    if ts.due or ts.path_due:
                        log.info("labeler tick: due=%d done(scan_row=%d, multi_price=%d) failed=%d pending=%d | "
                                 "path due=%d done=%d failed=%d budget_skip=%d | cu=%d",
                                 ts.due, ts.done_scan_row, ts.done_multi_price, ts.failed, ts.still_pending,
                                 ts.path_due, ts.path_done, ts.path_failed, ts.path_skipped_budget, ts.cu)
                except Exception:  # noqa: BLE001
                    log.exception("labeler tick crashed")
                if once:
                    return
                await asyncio.sleep(labeler.tick_interval_s)

        eval_lock = asyncio.Lock()   # tape loop and immediate-on-nomination evaluations must not interleave

        async def poll_and_evaluate(active: list[tuple[ChainConfig, str]], reason: str) -> None:
            async with eval_lock:
                ps = await poller.poll(active)
                tape_totals["polls"] += ps.polled
                tape_totals["fetched"] += ps.fetched
                tape_totals["new"] += ps.new
                tape_totals["cu"] += ps.cu
                tape_totals["budget"] += ps.skipped_budget
                tape_totals["errors"] += ps.errors
                log.info("tape poll (%s): active=%d polled=%d skipped(refresh=%d, budget=%d) fetched=%d new=%d "
                         "errors=%d cu=%d (tape cu today=%d/%d)", reason, ps.active, ps.polled, ps.skipped_refresh,
                         ps.skipped_budget, ps.fetched, ps.new, ps.errors, ps.cu, poller.cu_today,
                         poller.daily_cu_budget)
                await evaluate_candidates(active)

        async def tape_loop() -> None:
            while True:
                try:
                    now_i = int(time.time())
                    for c in manager.expire(now_i):
                        store.drop(c.chain, c.address)
                        log.info("candidate expired: %s %s after %d polls", c.chain, c.symbol or c.address[:8], c.polls)
                    active = [(chain_map[c.chain], c.address) for c in manager.active(now=now_i)]
                    if active:
                        await poll_and_evaluate(active, "tick")
                except Exception:  # noqa: BLE001
                    log.exception("tape poll crashed")
                if once:
                    return
                await asyncio.sleep(poller.interval_s)

        async def evaluate_candidates(active: list[tuple[ChainConfig, str]]) -> None:
            # Stage-2 features per active candidate (pure function of the tape; persisted per poll)
            feat_cfg = stage2_cfg.get("features", {})
            eval_ts = int(time.time())
            for ch, address in active:
                tape = store.get(ch.name, address)
                if len(tape) == 0:
                    continue
                trades = tape.trades()
                f = compute_features(trades, feat_cfg)
                # enrichments are awaited OUTSIDE any transaction (network); cached + budgeted
                holdings = await enricher.holdings(ch, address)
                flows = await enricher.tag_flows(ch, address)
                w = evaluate_wash(trades, f, wash_cfg, liquidity=latest_liquidity(ch.name, address),
                                  holdings_pct=holdings, tag_flows=flows)
                cand = manager.active_set.get((ch.name, address))
                s1_feats, s1_ts = latest_stage1_features(conn, ch.name, address)
                sr = await safety.check(ch, address)          # cached 10 min; RPC + one 25-CU holder profile
                safety_totals[sr.verdict] = safety_totals.get(sr.verdict, 0) + 1
                d = decide(ch.name, address, f, w, scoring_cfg, eval_ts, s1_features=s1_feats, s1_ts=s1_ts,
                           fallback_anchor_ts=cand.anchor_ts if cand else None,
                           safety_bonus=sr.bonus, safety_verdict=sr.verdict, safety_reasons=sr.reasons,
                           safety_flags=sr.flags)
                if not sr.from_cache:
                    log.info("safety %s %s: %s bonus=%.0f reasons=%s flags=%s", ch.name,
                             (cand.symbol if cand and cand.symbol else address[:8]), sr.verdict, sr.bonus,
                             ",".join(sr.reasons) or "-", ",".join(sr.flags) or "-")
                conn.execute("BEGIN")
                try:
                    tf_id = persist_features(conn, ch.name, address, f, eval_ts, wash=w)
                    persist_decision(conn, d, tape_features_id=tf_id)
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                decision_totals[d.tier] = decision_totals.get(d.tier, 0) + 1
                if d.alertable:
                    decision_totals["alertable"] = decision_totals.get("alertable", 0) + 1
                outcome = manager.on_tape(ch, address, f.ofi30.ofi, d.hard_vetoes, eval_ts,
                                          anchor_ts=f.anchor_ts, anchor_price=f.anchor_price)
                if outcome == "vetoed":
                    store.drop(ch.name, address)
                log.info(format_decision_line(d, cand.symbol if cand else None))
                log.info("features %s %s: n=%d ofi30=%s recent=%s buyers=%d/%d new=%s anchor=%s "
                         "vs_avwap=%s vs_anchor=%s clv=%s hl=%s | wash=%s veto=%s flag=%s%s",
                         ch.name, address[:8], f.n, _fmt(f.ofi30.ofi), _fmt(f.recent.ofi),
                         f.ofi30.buyers, f.ofi30.sellers, _fmt(f.ofi30.new_wallet_share_usd),
                         f"{f.seconds_since_anchor}s" if f.anchor_ts else "-",
                         _fmt(f.price_vs_avwap_pct, "%"), _fmt(f.price_vs_anchor_pct, "%"),
                         _fmt(f.clv_last), f.higher_lows_2of3, _fmt(w.wash_score),
                         ",".join(w.hard_vetoes) or "-", ",".join(w.soft_flags) or "-",
                         " -> REMOVED" if outcome == "vetoed" else "")
            if enricher.calls or enricher.cache_hits:
                log.info("enrichment: calls=%d cache_hits=%d budget_skips=%d cu_today=%d/%d",
                         enricher.calls, enricher.cache_hits, enricher.budget_skips,
                         enricher.cu_today, enricher.daily_cu_budget)

        tasks = [asyncio.create_task(chain_loop(ch), name=f"chain:{ch.name}") for ch in cfg.enabled_chains]
        tasks.append(asyncio.create_task(label_loop(), name="labeler"))
        tasks.append(asyncio.create_task(tape_loop(), name="tape"))
        try:
            if once:
                await asyncio.gather(*tasks)
            elif duration_s is not None:
                await asyncio.wait(tasks, timeout=duration_s)
            else:
                await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await safety.close()
        label_counts = labeler.counts()

    rh_total = rh_5m = None
    if "robinhood" in stats:
        rh_total = int(conn.execute("SELECT COUNT(*) FROM scan_rows WHERE chain='robinhood' AND ts>=?",
                                    (int(started),)).fetchone()[0])
        rh_5m = int(conn.execute("SELECT COUNT(*) FROM scan_rows WHERE chain='robinhood' AND ts>=? "
                                 "AND vol_5m IS NOT NULL", (int(started),)).fetchone()[0])
    summary = RunSummary(started_ts=started, ended_ts=time.time(), stats=stats,
                         cu_today_total=ledger.today_total(), projected_cu_per_day=projected_cu_per_day(cfg),
                         rh_rows_with_5m=rh_5m, rh_rows_total=rh_total,
                         label_ticks=label_totals["ticks"], labels_done=label_totals["done"],
                         labels_failed=label_totals["failed"], paths_done=label_totals["paths"],
                         label_counts=label_counts,
                         tape_polls=tape_totals["polls"], tape_fetched=tape_totals["fetched"],
                         tape_new=tape_totals["new"], tape_cu=tape_totals["cu"],
                         tape_skipped_budget=tape_totals["budget"], tape_errors=tape_totals["errors"],
                         candidate_stats={k: v for k, v in vars(manager.stats).items() if k != "events"},
                         decision_totals=dict(decision_totals), safety_totals=dict(safety_totals))
    conn.close()
    return summary


def format_summary(s: RunSummary, cfg: Config) -> str:
    lines = [f"run summary: {int(s.ended_ts - s.started_ts)}s wall, plan={cfg.birdeye_plan}"]
    for name, st in s.stats.items():
        lines.append(f"  {name:<10} cycles={st.cycles} ok={st.ok} err={st.errors} skipped={st.skipped} "
                     f"fetched={st.fetched} persisted={st.persisted} violations={st.violations} "
                     f"dup={st.duplicates} cu={st.cu}")
        for v in st.violation_samples:
            lines.append(f"             violation sample: {v}")
        hours = max(1e-9, (s.ended_ts - s.started_ts) / 3600)
        top_fails = sorted(st.s1_fail_counts.items(), key=lambda kv: -kv[1])[:4]
        lines.append(f"             stage1: evaluated={st.s1_evaluated} passed={st.s1_passed} "
                     f"nominated={st.s1_nominated} ({st.s1_nominated / hours:.1f}/h) cooldown={st.s1_cooldown} "
                     f"first-fail={dict(top_fails)}")
        if st.s1_nominees:
            lines.append(f"             nominees: {', '.join(sym for sym, _ in st.s1_nominees[:12])}")
        lines.append(f"             labeler: rows created={st.labels_created} controls drawn={st.controls_drawn}")
    lines.append(f"  labeler ticks={s.label_ticks} done={s.labels_done} failed={s.labels_failed} paths={s.paths_done}; "
                 f"label table: {s.label_counts}")
    lines.append(f"  tape polls={s.tape_polls} fetched={s.tape_fetched} new={s.tape_new} cu={s.tape_cu} "
                 f"budget_skips={s.tape_skipped_budget} errors={s.tape_errors}")
    if s.candidate_stats:
        lines.append(f"  candidates: {s.candidate_stats}")
    if s.decision_totals:
        lines.append(f"  decisions: {s.decision_totals}")
    if s.safety_totals:
        lines.append(f"  safety verdicts: {s.safety_totals}")
    daily_cap = int(cfg.raw.get("birdeye", {}).get("daily_cu_cap") or daily_cu_budget(cfg.birdeye_plan))
    lines.append(f"  CU today={s.cu_today_total} cap={daily_cap}; projected Stage-0 CU/day={s.projected_cu_per_day} "
                 f"({'within' if s.projected_cu_per_day <= daily_cap else 'EXCEEDS'} cap)")
    if s.rh_rows_total is not None:
        lines.append(f"  robinhood rows with vol_5m present: {s.rh_rows_with_5m}/{s.rh_rows_total}")
    return "\n".join(lines)
