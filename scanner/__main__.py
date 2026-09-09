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


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    parser = argparse.ArgumentParser(prog="scanner", description="Momentum Ignition Scanner")
    parser.add_argument("command", choices=["selftest", "smoke", "run", "replay"])
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--env", type=Path, default=ENV_PATH)
    parser.add_argument("--duration", type=float, default=None, help="run: stop after N seconds")
    parser.add_argument("--once", action="store_true", help="run: one cycle per chain, then exit")
    parser.add_argument("--chain", action="append", default=None, help="replay: restrict to chain (repeatable)")
    args = parser.parse_args(argv)
    if args.command == "selftest":
        return cmd_selftest(args.config, args.env)
    if args.command == "smoke":
        return cmd_smoke(args.config, args.env)
    if args.command == "replay":
        return cmd_replay(args.config, args.env, args.chain)
    return cmd_run(args.config, args.env, args.duration, args.once)


if __name__ == "__main__":
    sys.exit(main())
