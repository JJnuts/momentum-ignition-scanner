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
from .features import compute as compute_features
from .tape import TapePoller, TapeStore, persist_features

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


def _fmt(v: float | None, suffix: str = "") -> str:
    return "-" if v is None else f"{v:.2f}{suffix}"


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
        stay_s = int(cfg.raw.get("candidates", {}).get("max_stay_min", 8)) * 60
        max_per_chain = int(cfg.raw.get("candidates", {}).get("max_per_chain", 12))

        async def provisional_active() -> list[tuple[ChainConfig, str]]:
            """Until T8: the most recent WATCH nominations per chain, within the stay window."""
            now = int(time.time())
            out: list[tuple[ChainConfig, str]] = []
            for ch in cfg.enabled_chains:
                rows = conn.execute(
                    "SELECT address, MAX(ts) AS ts FROM nominations WHERE chain=? AND tier='WATCH' AND ts>=? "
                    "GROUP BY address ORDER BY ts DESC LIMIT ?", (ch.name, now - stay_s, max_per_chain)).fetchall()
                out.extend((ch, r["address"]) for r in rows)
            return out

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

        async def tape_loop() -> None:
            while True:
                try:
                    active = await provisional_active()
                    if active:
                        ps = await poller.poll(active)
                        tape_totals["polls"] += ps.polled
                        tape_totals["fetched"] += ps.fetched
                        tape_totals["new"] += ps.new
                        tape_totals["cu"] += ps.cu
                        tape_totals["budget"] += ps.skipped_budget
                        tape_totals["errors"] += ps.errors
                        log.info("tape poll: active=%d polled=%d skipped(refresh=%d, budget=%d) fetched=%d new=%d "
                                 "errors=%d cu=%d (tape cu today=%d/%d)", ps.active, ps.polled, ps.skipped_refresh,
                                 ps.skipped_budget, ps.fetched, ps.new, ps.errors, ps.cu, poller.cu_today,
                                 poller.daily_cu_budget)
                        # Stage-2 features per active candidate (pure function of the tape; persisted per poll)
                        feat_cfg = cfg.raw.get("stage2", {}).get("features", {})
                        eval_ts = int(time.time())
                        conn.execute("BEGIN")
                        try:
                            for ch, address in active:
                                tape = store.get(ch.name, address)
                                if len(tape) == 0:
                                    continue
                                f = compute_features(tape.trades(), feat_cfg)
                                persist_features(conn, ch.name, address, f, eval_ts)
                                log.info("features %s %s: n=%d ofi30=%s recent=%s buyers=%d/%d new=%s anchor=%s "
                                         "vs_avwap=%s vs_anchor=%s clv=%s hl=%s",
                                         ch.name, address[:8], f.n, _fmt(f.ofi30.ofi), _fmt(f.recent.ofi),
                                         f.ofi30.buyers, f.ofi30.sellers, _fmt(f.ofi30.new_wallet_share_usd),
                                         f"{f.seconds_since_anchor}s" if f.anchor_ts else "-",
                                         _fmt(f.price_vs_avwap_pct, "%"), _fmt(f.price_vs_anchor_pct, "%"),
                                         _fmt(f.clv_last), f.higher_lows_2of3)
                            conn.execute("COMMIT")
                        except Exception:
                            conn.execute("ROLLBACK")
                            raise
                except Exception:  # noqa: BLE001
                    log.exception("tape poll crashed")
                if once:
                    return
                await asyncio.sleep(poller.interval_s)

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
                         tape_skipped_budget=tape_totals["budget"], tape_errors=tape_totals["errors"])
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
    daily_cap = int(cfg.raw.get("birdeye", {}).get("daily_cu_cap") or daily_cu_budget(cfg.birdeye_plan))
    lines.append(f"  CU today={s.cu_today_total} cap={daily_cap}; projected Stage-0 CU/day={s.projected_cu_per_day} "
                 f"({'within' if s.projected_cu_per_day <= daily_cap else 'EXCEEDS'} cap)")
    if s.rh_rows_total is not None:
        lines.append(f"  robinhood rows with vol_5m present: {s.rh_rows_with_5m}/{s.rh_rows_total}")
    return "\n".join(lines)
