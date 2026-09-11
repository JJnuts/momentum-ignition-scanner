"""CLI entry point.

  python -m scanner selftest   - offline self-check (config, .env, DB, recorder, logging)
  python -m scanner run        - main loop (implemented from T2 onward)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from . import __version__
from .config import CONFIG_PATH, ENV_PATH, ConfigError, load_config, mask_secret, resolve_env_path
from .db import EXPECTED_TABLES, open_db, schema_version, table_names
from .logging_setup import setup_logging
from .plans import PLANS, daily_cu_budget, endpoint_available
from .recorder import RawRecorder

log = logging.getLogger("scanner")


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def cmd_selftest(config_path: Path, env_path: Path) -> int:
    print(f"Momentum Ignition Scanner v{__version__} - selftest")
    print(f"  config: {config_path}")
    env_used = resolve_env_path(env_path)
    print(f"  env:    {env_used}{'' if env_used.exists() else '  (not found)'}")

    # 1. config + secrets
    try:
        cfg = load_config(config_path, env_path)
    except ConfigError as e:
        _fail(f"config: {e}")
        return 2
    _ok(f"config loaded; birdeye plan={cfg.birdeye_plan} "
        f"(rps={PLANS[cfg.birdeye_plan]['rps']}, daily CU budget={daily_cu_budget(cfg.birdeye_plan):,})")
    _ok(f"BIRDEYE_API_KEY present: {mask_secret(cfg.secret('BIRDEYE_API_KEY'))}")
    for k in ("DISCORD_WEBHOOK_TEST", "DISCORD_WEBHOOK_LIVE"):
        v = cfg.secret(k)
        print(f"  [--]   {k}: {'set' if v else 'not set'}")
    for ch in cfg.chains.values():
        state = "enabled" if ch.enabled else "disabled"
        _ok(f"chain {ch.name}: {state}, birdeye_chain={ch.birdeye_chain}, "
            f"interval={ch.scan_interval_s}s, stage0 keys={len(ch.stage0)}, stage1 keys={len(ch.stage1)}")
    gated = [e for e in ("token_security", "token_creation_info", "websocket")
             if not endpoint_available(e, cfg.birdeye_plan)]
    if gated:
        print(f"  [--]   endpoints NOT available on plan '{cfg.birdeye_plan}': {', '.join(gated)} (by design; see SPEC section 2)")

    # 2. logging
    try:
        log_path = setup_logging(cfg.log_dir)
        log.info("selftest: logging initialised")
    except Exception as e:  # noqa: BLE001
        _fail(f"logging: {e}")
        return 3
    _ok(f"logging -> {log_path}")

    # 3. database
    try:
        conn = open_db(cfg.db_path)
        names = table_names(conn)
        missing = EXPECTED_TABLES - names
        if missing:
            _fail(f"db: missing tables {sorted(missing)}")
            return 4
        ver = schema_version(conn)
        conn.execute("INSERT INTO meta(key, value) VALUES('selftest_ts', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(int(time.time())),))
        conn.close()
    except Exception as e:  # noqa: BLE001
        _fail(f"db: {e}")
        return 4
    _ok(f"db -> {cfg.db_path} (schema v{ver}, {len(names)} tables)")

    # 4. raw recorder round-trip
    try:
        rec = RawRecorder(cfg.raw_dir, enabled=cfg.recorder_enabled)
        if cfg.recorder_enabled:
            path = rec.record("selftest", {"k": "v"}, 200, {"ok": True}, chain=None, latency_ms=0)
            last = None
            for last in rec.iter_file(path):
                pass
            if not last or last.get("endpoint") != "selftest":
                _fail("recorder: round-trip mismatch")
                return 5
            _ok(f"recorder -> {path.name} (round-trip verified)")
        else:
            print("  [--]   recorder disabled in config (NOT recommended)")
    except Exception as e:  # noqa: BLE001
        _fail(f"recorder: {e}")
        return 5

    log.info("selftest passed")
    print("SELFTEST PASSED")
    return 0


def cmd_run(config_path: Path, env_path: Path, duration_s: float | None, once: bool) -> int:
    import asyncio

    from .runner import format_summary, run_loop

    try:
        cfg = load_config(config_path, env_path)
    except ConfigError as e:
        print(f"config error: {e}")
        return 2
    setup_logging(cfg.log_dir)
    mode = "once" if once else (f"{int(duration_s)}s" if duration_s else "forever")
    print(f"Momentum Ignition Scanner v{__version__} - run ({mode}, plan={cfg.birdeye_plan})")
    pid_file = cfg.db_path.parent / "scanner.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    import os
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    try:
        summary = asyncio.run(run_loop(cfg, duration_s=duration_s, once=once))
    except KeyboardInterrupt:
        print("interrupted")
        return 130
    finally:
        try:
            if pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_file.unlink()
        except OSError:
            pass
    text = format_summary(summary, cfg)
    print(text)
    log.info(text.replace("\n", " | "))
    errors = sum(st.errors for st in summary.stats.values())
    return 0 if errors == 0 else 1


def cmd_smoke(config_path: Path, env_path: Path) -> int:
    import asyncio

    from .smoke import run_smoke

    try:
        cfg = load_config(config_path, env_path)
    except ConfigError as e:
        print(f"config error: {e}")
        return 2
    setup_logging(cfg.log_dir)
    print(f"Momentum Ignition Scanner v{__version__} - live Birdeye smoke (plan={cfg.birdeye_plan}, "
          f"key={mask_secret(cfg.secret('BIRDEYE_API_KEY'))})")
    return asyncio.run(run_smoke(cfg))


def cmd_replay(config_path: Path, env_path: Path, chains: list[str] | None) -> int:
    from .replay import format_replay, replay

    try:
        cfg = load_config(config_path, env_path)
    except ConfigError as e:
        print(f"config error: {e}")
        return 2
    setup_logging(cfg.log_dir)
    res = replay(cfg, chains=chains)
    print(format_replay(res))
    return 0


def cmd_tape_check(config_path: Path, env_path: Path, chain: str | None, address: str | None, pages: int) -> int:
    """T5 acceptance: fetch a token's tape live, verify ordering/dedupe, compare 5-min USD sum with the list row."""
    import asyncio
    import time as _time

    from .birdeye import BirdeyeClient
    from .db import open_db
    from .ledger import CULedger
    from .recorder import RawRecorder
    from .tape import TapeStore, fetch_tape

    try:
        cfg = load_config(config_path, env_path)
    except ConfigError as e:
        print(f"config error: {e}")
        return 2
    setup_logging(cfg.log_dir)
    conn = open_db(cfg.db_path)
    chain = chain or "solana"
    ch = cfg.chains[chain]
    if address is None:
        # a MID-activity recent token: enough trades to measure, few enough that 3 pages (300 trades)
        # cover at least 5 minutes. SOL/USDC-class tokens trade hundreds of times per second.
        if chain == "solana":
            row = conn.execute("SELECT address, symbol FROM scan_rows WHERE chain=? AND ts>=? AND tr_5m BETWEEN 20 AND 200 "
                               "ORDER BY vol_5m DESC LIMIT 1", (chain, int(_time.time()) - 900)).fetchone()
        else:
            row = conn.execute("SELECT address, symbol FROM scan_rows WHERE chain=? AND ts>=? AND tr_1h BETWEEN 100 AND 2000 "
                               "ORDER BY vol_1h DESC LIMIT 1", (chain, int(_time.time()) - 900)).fetchone()
        if row is None:
            print("no recent scan rows; pass --address")
            return 1
        address = row["address"]
        print(f"token: {row['symbol']} {address}")

    async def go() -> int:
        ledger = CULedger(conn)
        async with BirdeyeClient(cfg.secret("BIRDEYE_API_KEY") or "", cfg.birdeye_plan, base_url=cfg.birdeye_base_url,
                                 recorder=RawRecorder(cfg.raw_dir, cfg.recorder_enabled), ledger=ledger,
                                 rate_safety=float(cfg.raw["birdeye"].get("rate_safety", 0.8))) as be:
            store = TapeStore(conn)
            r1 = await fetch_tape(be, store, ch, address, max_pages=pages)
            print(f"fetch 1: pages={r1.pages} fetched={r1.fetched} new={r1.new} dup={r1.dup} cu={r1.cu} stop={r1.stopped}"
                  + (f" ERROR {r1.error}" if r1.error else ""))
            r2 = await fetch_tape(be, store, ch, address, max_pages=1)
            print(f"fetch 2 (incremental): fetched={r2.fetched} new={r2.new} dup={r2.dup} cu={r2.cu} stop={r2.stopped}")
            tape = store.get(ch.name, address)
            ts = [t.ts for t in tape.trades()]
            sigs = [t.sig for t in tape.trades()]
            print(f"tape: {len(tape)} trades, span {tape.oldest_ts}..{tape.newest_ts} "
                  f"({(tape.newest_ts or 0) - (tape.oldest_ts or 0)} s), sorted={ts == sorted(ts)}, "
                  f"unique sigs={len(set(sigs)) == len(sigs)}")
            now = int(_time.time())
            covered_5m = tape.oldest_ts is not None and tape.oldest_ts <= now - 300
            s5 = tape.sum_usd(now - 300)
            b5 = tape.sum_usd(now - 300, "buy")
            print(f"last 5m: trades={tape.count(now - 300)} usd={s5:,.0f} (buy {b5:,.0f}) "
                  f"unique wallets={tape.unique_wallets(now - 300)} tape covers full 5m={covered_5m}")
            row = conn.execute("SELECT vol_5m, vol_1h, ts FROM scan_rows WHERE chain=? AND address=? ORDER BY ts DESC LIMIT 1",
                               (chain, address)).fetchone()
            if row and row["vol_5m"] is not None and covered_5m:
                ref = float(row["vol_5m"])
                diff = (s5 / ref - 1) * 100 if ref else float("nan")
                print(f"list-row vol_5m={ref:,.0f} (age {now - int(row['ts'])} s) -> tape/list diff {diff:+.1f}% "
                      f"({'OK' if abs(diff) <= 15 else 'OUTSIDE +-15%'})")
            elif row and row["vol_1h"] is not None:
                print(f"(no 5m field on this chain) list-row vol_1h={float(row['vol_1h']):,.0f}; "
                      f"tape 1h usd={tape.sum_usd(now - 3600):,.0f} covers 1h={tape.oldest_ts is not None and tape.oldest_ts <= now - 3600}")
            print(f"CU: {ledger.session_cu}")
            dbn = conn.execute("SELECT COUNT(*) FROM trades WHERE chain=? AND address=?", (chain, address)).fetchone()[0]
            print(f"persisted rows for token: {dbn}")
            return 0

    return asyncio.run(go())


def cmd_tune(config_path: Path, env_path: Path, since_days: float | None, out: Path | None) -> int:
    import time as _time

    from .db import open_db
    from .tune import build_report

    try:
        cfg = load_config(config_path, env_path)
    except ConfigError as e:
        print(f"config error: {e}")
        return 2
    conn = open_db(cfg.db_path)
    since = int(_time.time() - since_days * 86400) if since_days else 0
    report = build_report(conn, cfg.raw.get("tune", {}), since_ts=since)
    conn.close()
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"report written: {out}")
    print(report)
    return 0


def cmd_backtest(config_path: Path, env_path: Path, since_days: float | None, limit: int | None, out: Path | None) -> int:
    import time as _time

    from .backtest import evaluate_paths, format_report, replay_decisions
    from .db import open_db

    try:
        cfg = load_config(config_path, env_path)
    except ConfigError as e:
        print(f"config error: {e}")
        return 2
    conn = open_db(cfg.db_path)
    since = int(_time.time() - since_days * 86400) if since_days else 0
    rep = replay_decisions(conn, cfg.raw, since_ts=since, limit=limit)
    paths = evaluate_paths(conn, cfg.raw_dir, cfg.raw, since_ts=since)
    text = format_report(rep, paths)
    conn.close()
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"report written: {out}")
    print(text)
    return 0 if rep.n == 0 or rep.decision_match_rate >= 0.99 else 1


def cmd_budget(config_path: Path, env_path: Path, since_days: float | None, out: Path | None) -> int:
    from . import clock as dayclock
    from .budget import build, format_report
    from .db import open_db
    from .plans import daily_cu_budget

    try:
        cfg = load_config(config_path, env_path)
    except ConfigError as e:
        print(f"config error: {e}")
        return 2
    dayclock.configure(cfg.timezone)
    daily_cap = int(cfg.raw.get("birdeye", {}).get("daily_cu_cap") or daily_cu_budget(cfg.birdeye_plan))
    conn = open_db(cfg.db_path)
    rep = build(conn, daily_cap=daily_cap, days=int(since_days or 3))
    conn.close()
    text = format_report(rep)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"report written: {out}")
    print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    parser = argparse.ArgumentParser(prog="scanner", description="Momentum Ignition Scanner")
    parser.add_argument("command", choices=["selftest", "smoke", "run", "replay", "tape-check", "tune", "backtest", "budget"])
    parser.add_argument("--limit", type=int, default=None, help="backtest: only the last N decisions")
    parser.add_argument("--since-days", type=float, default=None, help="tune/backtest: only events from the last N days; budget: local days to show (default 3)")
    parser.add_argument("--out", type=Path, default=None, help="tune: also write the markdown report here")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--env", type=Path, default=ENV_PATH)
    parser.add_argument("--duration", type=float, default=None, help="run: stop after N seconds")
    parser.add_argument("--once", action="store_true", help="run: one cycle per chain, then exit")
    parser.add_argument("--chain", action="append", default=None, help="replay: restrict to chain (repeatable)")
    parser.add_argument("--address", default=None, help="tape-check: token address (default: hottest recent)")
    parser.add_argument("--pages", type=int, default=3, help="tape-check: pages on first fetch")
    args = parser.parse_args(argv)
    if args.command == "selftest":
        return cmd_selftest(args.config, args.env)
    if args.command == "smoke":
        return cmd_smoke(args.config, args.env)
    if args.command == "replay":
        return cmd_replay(args.config, args.env, args.chain)
    if args.command == "tape-check":
        return cmd_tape_check(args.config, args.env, (args.chain or [None])[0], args.address, args.pages)
    if args.command == "tune":
        return cmd_tune(args.config, args.env, args.since_days, args.out)
    if args.command == "backtest":
        return cmd_backtest(args.config, args.env, args.since_days, args.limit, args.out)
    if args.command == "budget":
        return cmd_budget(args.config, args.env, args.since_days, args.out)
    return cmd_run(args.config, args.env, args.duration, args.once)


if __name__ == "__main__":
    sys.exit(main())
