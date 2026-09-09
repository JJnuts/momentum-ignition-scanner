# STATUS

Task ledger. A task is DONE only when its acceptance test passed in front of the user.
See ROADMAP.md for task definitions, SPEC.md for design.

| task | status | verified | notes |
|---|---|---|---|
| T0 skeleton | DONE | 2026-09-08 | 25/25 pytest; selftest fails loudly without .env (exit 2) and passes with a key (exit 0) |
| T1 Birdeye client | DONE | 2026-09-08 | 44/44 pytest; live smoke PASSED both chains, 300 CU, 0 retries after rate_safety fix |
| T2 Stage 0 scan | DONE | 2026-09-08 | 59/59 pytest; 10-min live run: SOL 10 cycles / RH 5 cycles, 1500 rows, 0 violations, 0 errors, 0 429s, 750 CU |
| T3 Stage 1 features | DONE | 2026-09-08 | 82/82 pytest; replay over 0.37h: SOL 26.8/h, RH 20.5/h cold-start; thresholds calibrated once and FROZEN (SPEC s14); live runner cycle persisted a WATCH nomination |
| T4 Labeler v1 | DONE | 2026-09-08 | 94/94 pytest; live: runner enqueued a nomination + a control, label loop ticked; real OHLCV path fill on AMC parsed correctly |
| Milestone A | RUNNING | started 2026-09-09 | Starter key; detached via start_scanner.bat / PowerShell Start-Process; stop with stop_scanner.bat; WATCH-only, thresholds frozen |
| T5–T9b Phase 2 | todo | | |
| T10–T12 Phase 3 | todo | | T10 = RPC-based (Birdeye security is Premium-only) |
| T13 Discord | todo | | |
| Milestone B | todo | | |
| T14–T15 Phase 5 | todo | | |

## T0 notes (2026-09-08)
- Layout: `scanner/` package (`config`, `plans`, `db`, `recorder`, `logging_setup`, `__main__`),
  `tests/` (pytest), `config.json`, `.env.example`, `run.bat`, `selftest.bat`, `pytest.ini`.
- DB schema v1: meta, scan_rows, nominations, candidates, trades, alerts, labels, safety, cu_ledger.
- Raw recorder: `raw/YYYY-MM-DD.jsonl.gz`, one JSON line per API response, multi-member gzip append.
- Plan table + endpoint gating + CU costs in `scanner/plans.py` (verified vs estimated flagged).
- Defects found and fixed during QA: (1) Windows console is cp1252 → all console output is ASCII and
  stdout is reconfigured to UTF-8 with replacement; (2) pytest tmp dir under %TEMP% was not writable in
  the build sandbox → `pytest.ini` pins `--basetemp=.pytest_tmp` (project-local).
- Open: `.env` not yet created by user. `db.path` and `recorder.dir` accept absolute paths if the
  user prefers to keep live SQLite/raw files outside the OneDrive-synced folder.

## T1 notes (2026-09-08)
- `scanner/birdeye.py`: gating -> token bucket -> HTTP -> raw record -> CU ledger -> retry (429 w/ Retry-After,
  5xx, timeouts; exponential backoff) -> `data`. Transport is injectable; tests run offline.
- Endpoint paths + verified CU in `plans.py`: list v3 50, ohlcv v3 45/75/100, txs v3 12 (limit<=100, free plan),
  txs v1 10 (limit<=50), trade-data 10, market-data 8, multi_price ceil(3n^0.8). `x-chain: robinhood` documented.
- CU is charged to the ledger on HTTP 200 only (assumption; compare with the Birdeye dashboard after the smoke).
- `python -m scanner smoke`: live acceptance (~300 CU): list/ohlcv/txs on both chains, gating check, and the
  Solana pre-migration probe (reads `source` on young small-cap tokens' trades for pump.fun).
- Secrets: loader accepts `.env`, `secrets.txt`, `secrets.env`, `env.txt` (first found). User keeps the master
  copy as `sniper.txt` on the Desktop; a copy lives in the project as `secrets.txt` (git-ignored).
- Live smoke run 1: PASSED but 5/15 calls hit 429 at exactly 1 rps -> added `birdeye.rate_safety` (0.8) and no
  burst on the 1 rps tier. Run 2: 10 calls, 0 retries, 300 CU, ledger matched the table exactly.
- Findings recorded in SPEC section 13: Robinhood list v3 has only 1h+ windows (no 1m/5m/30m); pre-migration
  launchpad tokens appear in Solana list with liquidity=0 (sources seen: pump_amm, raydium_launchlab); Birdeye
  sends x-ratelimit-* headers (limit=100 on a longer window).

## T2 notes (2026-09-08)
- `scanner/stage0.py`: config filters passed verbatim to list v3 (+ alive window), TokenRow normalisation
  (missing fields -> NULL), client-side re-verification of every min_/max_ filter, per-chain cycle ids in meta,
  alternating sort keys, daily CU budget guard before each call.
- `scanner/runner.py`: one asyncio task per chain on its own interval; `run --once` / `run --duration N`.
- DB schema v2 via the migration mechanism (v1 CREATE statements frozen; MIGRATIONS[2] adds 24h fields,
  sort_key, rank). Tested: v1 DB migrates in place, data survives, newer schema is refused.
- Defects found and fixed during QA: (1) Robinhood `holder` is always null -> `min_holder` returned 0 rows;
  removed for RH. (2) 429 at 1.3 s spacing on the free tier -> rate_safety 0.5. (3) Free daily cap (900)
  would have blocked the acceptance run -> explicit `birdeye.daily_cu_cap` (3000) for free-tier dev.
- Acceptance: 600 s run, 15 cycles, 1500 rows persisted, 0 violations, 0 errors, 750 CU. 260 distinct Solana
  tokens and 228 distinct Robinhood tokens seen; 140 Solana rows in the 20k-100k mcap band; 948/1200 Solana rows
  carry a listing timestamp. Robinhood vol_5m present in 0/500 rows (field absent, not null).
- Note: projected Stage-0 CU/day at full cadence is 108k -> free tier cannot run continuously (expected;
  Milestone A needs Lite/Starter).

## T3 notes (2026-09-08)
- `scanner/stage1.py`: two data-driven modes. short (Solana): rVol_1m/5m with a baseline that EXCLUDES the
  current window and shrinks to token age, impact efficiency, turnover-wash veto, price bands, page
  percentile ranks, cohort z (ranker), holder growth vs the ~5-min-ago snapshot (soft). hourly (Robinhood):
  ignition from vol_1h / trade_1h deltas between consecutive polls, fallback on volume_1h_change_percent for
  page entrants with an extension cap. All DB lookups are ts < now (no lookahead); replay == live.
- WATCH nominations -> `nominations` (features_json, gates_json), 20-min per-token cooldown persisted across
  restarts. Runner logs `stage1: evaluated/passed/nominated/cooldown` per cycle.
- `scanner/replay.py` + `python -m scanner replay [--chain X]`: re-evaluates Stage 1 over stored rows, reports
  nominations/h, first-failing-gate distribution, nominee feature snapshots. Zero CU.
- Calibration (allowed once, now frozen; details SPEC s14): rvol_1m_min 4 -> 1.5 (recency, not ignition);
  page-percentile gates disabled (page mixes mcap tiers); RH fallback tightened (2000% / 50 trades / +10%
  / vol_1h >= 5k / entrant extension <= 80%), RH delta gates 4x / +8 trades.
- Acceptance: replay SOL 26.8/h (target 5-30), RH 20.5/h cold start (target 1-10; re-check at Milestone A,
  RH Stage 1 is a coarse pre-filter for tape polling). Live `run --once` through the runner: Stage 1 executed
  after Stage 0 on both chains and persisted 1 WATCH nomination (CME, robinhood) with 517-byte features and
  549-byte gates JSON.
- CU: 2800 of today's 3000 cap used (smoke + T2 + T3 collection). Monthly free budget used: ~2.8k of 30k.

## T4 notes (2026-09-08)
- `scanner/labeler.py`: `on_cycle` enqueues 4 label rows per nomination and draws seeded CONTROL samples
  (stored as tier=CONTROL nominations with features); `tick` runs the close pass (scan_rows -> multi_price ->
  retry -> failed after grace) and the path pass (OHLCV 1m at the final horizon, nominations only, budget-
  guarded, attempts/deadline). Schema v3 adds attempts/path_status/source to `labels`.
- Runner: label loop every 30 s alongside the chain loops; summary prints label table counts.
- Defects found and fixed during QA: (1) field name clash `Stage1Stats.nominated` (int vs list) -> list renamed
  `nominated_rows`; (2) a label whose close pass FAILED stayed failed after the OHLCV path recovered its close
  -> now flips to done (source=ohlcv).
- Live checks (145 CU): `run --once` enqueued LEVERHEDGE (solana) + 1 RH control -> 8 pending labels; scratch-DB
  path fill on the real AMC ignition (t0 14:25): +9.3% / +32.7% / +65.9% at +5/+15/+30, MAE +1.4%.
- CU today 2945/3000. Free budget for the month ~2.9k of 30k used.

## Starter activation + Milestone A start (2026-09-09)
- Key: user's master copy is Desktop `scraper-sniper-v1.txt` (raw key). `secrets.txt` rebuilt by shell (never
  echoed): BIRDEYE_API_KEY + DISCORD_WEBHOOK_TEST. config: plan=starter, rate_safety 0.8 (12 rps),
  daily_cu_cap null -> derived 240k/day.
- Smoke on Starter: 12 calls, 0 retries, 0 limiter waits, 320 CU; trade_data_single now available (5m
  buy/sell/unique_wallet fields) -> Stage 2 can use it as a cheap 10-CU confirmation source.
- PnL rate-limit probe: 40x wallet_pnl_summary in 11.4 s, 0 x 429 -> PnL endpoints are NOT under the 30 rpm
  wallet-group cap. Response is nested: data.summary.{unique_tokens, counts, cashflow_usd, pnl}.
  top_traders rows: owner, realizedPnl, totalPnl, tags, hold*, first/lastTradeUnixTime.
- Client: wrappers for wallet_pnl_summary, token_top_traders, token_holder_profile, wallet_tags_tracker,
  token_first_buyers, smart_money_token_list, wallet_identity; plan table + CU costs updated.
- Ops: `python -m scanner run` writes data/scanner.pid; start_scanner.bat (detached, minimized) and
  stop_scanner.bat (taskkill by PID). NOTE: from Git Bash use `cmd.exe //c` (MSYS converts `/c` to `C:\`).
- Milestone A goal: n >= 100 nominations per chain with labels; no threshold changes; review nomination
  rate, CU/day vs 240k cap, label completion rate, and the RH 1-10/h target.

## Commands
    python -m pytest              # unit tests
    python -m scanner selftest    # offline self-check (or double-click selftest.bat)
    python -m scanner run         # main loop (from T2)
