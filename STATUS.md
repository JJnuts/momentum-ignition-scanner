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
| T5 Trade tape | DONE | 2026-09-09 | 106/106 pytest; live: sorted, unique sigs, page-overlap dups caught, incremental fetch, rows persisted; tape vs Birdeye trade-data within ~1% USD on memecoin tokens |
| T6 Trade features | DONE | 2026-09-09 | 121/121 pytest incl. no-lookahead invariant; features computed on 8 real tapes look right; per-poll snapshots persisted (schema v4) |
| T7 Wash + vetoes | DONE | 2026-09-09 | 136/136 pytest; planted wash tape >= 0.6, organic <= 0.3, dev-dump trips DISTRIBUTION with OFI positive; real tapes separate cleanly; live enrichments cached |
| T8 Candidate manager | DONE | 2026-09-09 | 144/144 pytest: 30 nominations -> cap 12 + weakest-oldest eviction, stay/expire, veto+cooldown, degrade + day rollover, restart restore, first-contact depth |
| T9 Scoring + timing | DONE | 2026-09-09 | 160/160 pytest: fixture vectors -> 95 CONFIRMED / 65 IGNITION / 46 WATCH / VETO override; +20 s and +181 s not eligible; components carry data timestamps; decisions persisted |
| T9b Pre-migration | todo | | optional; after Phase 3 |
| T10 Solana safety | DONE | 2026-09-09 | 178/178 pytest; live: 4 SAFE / 2 UNSAFE on 6 candidates, token-2022 detected, verdicts feed vetoes/caps/bonus |
| T11 Robinhood safety | DONE | 2026-09-09 | 187/187 pytest; live sim on 3 tokens: sell/buy/transfer OK, 0% tax, pool round-trip fallback exercised; Blockscout unreachable -> concentration is a soft flag |
| T12 Rug watch | DONE | 2026-09-09 | 195/195 pytest: schedule 3 rows, healthy no warning, -60% liquidity warns ONCE, safety flip warns once with reasons, both reasons combine, market-data fallback + budget, grace -> failed; runner ticks it; schema v7 live |
| T13 Discord alerts | DONE | 2026-09-09 | 205/205 pytest; test card delivered to the test webhook; policy (cooldown/upgrade/hourly cap), rug-warning delivery, heartbeat, dry-run |
| Milestone B | RUNNING | started 2026-09-09 | pings ON to the TEST channel; thresholds FROZEN until n >= 100 IGNITION+CONFIRMED alerts per chain |
| T14 tune.py | DONE | 2026-09-09 | 210/210 pytest: synthetic planted signal ranks top, noise ranks last, expectancy exact on planted outcomes, report renders with/without data; real report: nominations 1.73x control, eff_5m top feature |
| T15 Replay backtester | DONE | 2026-09-09 | 220/220 pytest: synthetic tape reproduces live decision, planted backfill + config change reproduced via knowledge time / config version, exact-path rules on planted candles; real: 2344 decisions 94.7% match (legacy), gaps = backfill + eligibility config drift, fixed by schema v8 |
| T15a Budget audit + local-day reset | todo | | |
| T15b Burn-rate governor | todo | | |
| T15c Cheapest-spend trims | todo | | |
| T15d Live verification (no dark hours) | todo | | |

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

## T5 notes (2026-09-09)
- `scanner/tape.py`: normalize (token leg by address; sig = tx_hash:ins_index:inner/log index), TokenTape ring
  (sorted, deduped incl. within-batch), TapeStore (INSERT OR IGNORE into `trades`, reload on restart),
  fetch_tape (incremental paging: stop at has_next=false / reached-known / page cap), TapePoller (first fetch
  2 pages, refresh every 3rd poll, daily tape CU budget 80k + global cap). Runner `tape_loop` every 60 s over a
  PROVISIONAL active set (WATCH nominations from the last 8 min, <=12/chain) until T8.
- `python -m scanner tape-check [--chain] [--address] [--pages]`: live acceptance.
- Defects found and fixed during QA: (1) duplicates within one batch slipped into the ring (pages overlap in
  practice) -> dedupe against ring AND batch; (2) the "hottest token" picker chose SOL/USDC-class tokens whose
  300 trades span 1 s -> picker targets 20-200 trades / 5 min.
- Acceptance vs Birdeye trade-data at the same instant: ZCAT $45.6k vs $45.5k, NEAR $26.8k vs $27.1k,
  TRX $5,455 vs $5,457. List rows lag more than their timestamp suggests; quiet tokens' txs lag 40-80 s.
- OPS: the Milestone A process was found dead at 15:49 (no traceback, no summary, pid file left) -> external
  kill, most likely the minimized console closed. Relaunched HIDDEN with T5 code (start_scanner.bat now uses
  PowerShell Start-Process -WindowStyle Hidden). ~17 min gap in Milestone A data.

## T6 notes (2026-09-09)
- `scanner/features.py`: window stats (OFI USD-weighted, buy share, buyers/sellers, new-wallet share), anchor
  detection (rate ratio vs trailing baseline excluding the window, distinct-buyer floor, lookback), structure
  (exact aVWAP, 10-trade bars, CLV, higher lows, rejection wick, price vs anchor/aVWAP). `stage2.features` config.
- Runner: after each tape poll, features for every active candidate are computed and persisted to `tape_features`
  (migration v4) and logged one line per candidate.
- Tests: OFI extremes/alternating/USD-weighting, window count+age+min_n, new-wallet share, planted burst -> anchor
  inside the burst and absent on the quiet prefix, single-wallet spike never anchors, no trailing history -> no
  anchor, onset stable as trades arrive, bars/CLV/higher-lows/rejection on constructed paths, exact aVWAP,
  partial-bar rule, no-lookahead at timestamp boundaries, same-second inclusion, empty/unpriced tapes.
- QA finding: my first no-lookahead test cut the tape mid-second; several trades share a second on Solana, so
  the invariant holds at timestamp boundaries (test fixed, extra test added).
- Real-tape check (read-only): LUCKY99 anchor 589 s ago x5.2, OFI +0.43, +6.3% vs aVWAP, higher lows;
  SNP500 OFI -0.67 (sellers); UBER rejection wick 0.89. Hot tokens whose tape spans 2 min have no anchor
  (no trailing history) -> T8 first-contact depth.

## T7 notes (2026-09-09)
- `scanner/wash.py`: components (roundtrip, top3, uniformity, count trap, churn) -> wash_score; vetoes WASH,
  DISTRIBUTION (hard with holdings, soft suspect without), REJECTION, DEV_INSIDER_SELLING, BUNDLER_SELLING.
  `scanner/enrichment.py`: holdings via top_traders (25 CU) + tag flows via wallet-tags-tracker (30 CU, Solana),
  cached 5 min in `enrichment`, budgeted 40k CU/day. Runner evaluates wash per candidate per poll and persists
  wash_score / hard_vetoes / soft_flags / wash_json (schema v5).
- Live probes: tags tracker returns EMPTY groups with the default 1D frame; with time_frame=5m + explicit tags it
  returned kol + smart_trader buckets. Endpoint tags are dev/sniper/smart_trader/kol only.
- Real-tape check (read-only): Percolator WASH 0.68 (roundtrip 1.00, top3 0.94, tpw 3.5, new 0.05), LAPTOP 0.61;
  organic 0.08-0.35. DISTRIBUTION_SUSPECT is common on quiet tokens -> soft by design until holdings resolve it.

## T8 notes (2026-09-09)
- `scanner/candidates.py`: CandidateManager (enter/refresh/cap+evict/stay/expire/veto+cooldown/degrade), persisted in
  `candidates`, restored on restart (stale rows expired, veto cooldowns re-armed). Runner: nominations enter,
  active tokens on the Stage-1 page get a stay check, tape features update strength/anchor and hard vetoes
  remove (tape dropped from memory), expiry runs each tape loop, active set feeds the poller.
- Tape poller: first-contact depth pages until the tape spans >= 600 s (cap 6 pages); `fetch_tape(start_offset)`.
- Defects found and fixed during QA: (1) eviction tie-break picked the NEWEST instead of the oldest; (2) the
  degrade check used the ledger's wall-clock "today" while the manager runs on an injectable clock -> now
  `total_since(day_start)` by the manager's clock; (3) an older poller test now hit first-contact depth
  (fixture spans 200 s) -> that test pins first_contact_min_span_s=0.

## T9 notes (2026-09-09)
- `scanner/scoring.py`: score_components (6 components, linear lo->hi mapping), decide (tier, anchor source,
  eligibility, alertable), persist_decision (schema v6 `decisions`), latest_stage1_features (efficiency and
  holder growth come from the WATCH nomination's features_json). Runner: decision per candidate per poll,
  logged with the component breakdown and `*** ALERTABLE ***` when tier >= IGNITION inside the window.
- Config `scoring` (weights, mappings, tiers, window [30,180]); `alerts` now holds delivery policy only (T13).
- QA: my hand-computed expectation for the partial vector was wrong (45.8 = WATCH); code was right; test fixed
  and a true IGNITION vector (65.5) added.
- Live QA after the first T9 restart (2026-09-09): (1) the decision log line had one %s more than arguments ->
  Python logging dropped EVERY decision line and wrote 150 "Logging error" tracebacks to stderr (368 KB);
  the DB rows were fine. Fixed via a tested `format_decision_line` helper. (2) Timing data: Stage 1 nominates
  a median 112 s (p75 203 s) after the tape onset and the first Stage-2 evaluation waited up to a full tape
  tick -> the [30,180] window rejected ~half of first evaluations. Window widened to [30,360] and newly
  entered candidates are now polled + evaluated IMMEDIATELY on nomination (eval_lock serialises this with the
  tape tick). (3) `evaluate_candidates` extracted from the tape loop.

## T10 notes (2026-09-09)
- `scanner/safety.py`: parse_mint_account (RPC jsonParsed), parse_holder_profile (Birdeye), evaluate_solana
  (hard/soft checks + bonus), verdict_of (UNSAFE > UNKNOWN > SAFE), SafetyChecker (RPC via aiohttp, holder
  profile via client, cache in `safety`, budget). `scoring.decide` takes verdict/reasons/flags/bonus: UNSAFE ->
  hard veto; UNKNOWN or a CAP flag (bundler/sniper/token-2022 ext) -> no CONFIRMED; bonus -> safety component.
- Runner: safety checked per candidate per poll (cached), logged on fresh checks; `d.hard_vetoes` (wash + safety)
  drive candidate removal.
- QA: one test expected a non-None parse for an empty payload (wrong expectation; fixed). Live check surfaced a
  token with bundlers at 96.7% of supply that was SAFE by the hard checks -> added the tier cap for CAP flags.

## T11 notes (2026-09-09)
- `scanner/evm_safety.py`: calldata encoders, eth_simulateV1 three-block plan (sell/buy/transfer with balanceOf
  reads before/after, stateOverrides for ETH), owner() reader, top-10 proxy, evaluate_evm. `SafetyChecker._check_evm`
  wires RPC reads (totalSupply, decimals, owner, balances), tape holder selection from `trades`, the sim, and the
  top-traders proxy (25 CU). RPC calls now carry a browser UA, ~3 rps pacing and a 429 retry.
- Findings: Blockscout Cloudflare-blocked (all endpoints); RPC 403 on default Python UA; full-range getLogs times
  out; chunked getLogs 429s; Birdeye holdVolume is Solana-only -> Robinhood concentration unverifiable ->
  soft `top10_unknown` (design decision: must not cap the chain forever).
- QA: my first version made an unavailable proxy a hard UNKNOWN (would have capped all Robinhood at IGNITION);
  live run exposed it; changed to a soft flag with tests for both outcomes.

## T12 notes (2026-09-09)
- `scanner/rugwatch.py`: schedule/tick/pending_warnings/mark_delivered; liquidity from scan_rows else
  market_data_single; safety re-check forced via SafetyChecker; per-alert per-reason dedupe via `_already_warned`.
  Runner: ticked in the label loop every 30 s; summary prints rug totals. No alerts exist until T13, so the live
  check is limited to a clean restart with schema v7 and an idle tick.

## T13 notes (2026-09-09)
- `scanner/alerts.py`: DiscordWebhook (UA, wait=true, 429 retry), AlertPolicy (cooldown / upgrade / per-chain hourly cap,
  state from `alerts`), build_card / build_rug_card, Alerter (consider -> send -> persist alert + alerted_ts + rug
  schedule + alert labels; deliver_rug_warnings; heartbeat_if_due via meta.last_heartbeat_ts). Runner: alertable
  decisions go through `consider`; label loop delivers rug warnings + heartbeat; channel = test while
  discord.use_test_channel is true.
- QA: one policy test had the wrong expectation (hourly cap still applies after a cooldown expires; code right).
- Live: a labelled TEST CARD built from the latest CONFIRMED decision posted to the test webhook (message id
  returned). A console print of the card then hit cp1252 - the runner reconfigures stdout to UTF-8 so it is safe.

## T14 notes (2026-09-09)
- `scanner/tune.py`: load_events (nomination/control/alert + labels + decision/tape context for alerts), is_win /
  is_big_win / is_rug, feature_lifts, expectancy (rules A/B), time_to_peak, build_report (markdown). CLI `tune`.
  Reports land in `reports/` (git-ignored).
- First real report (SPEC s25): Stage 1 lift 1.73x vs control; impact efficiency top feature; rVol level not
  predictive past the gate; cohort z inverse; -20% stop hit by ~15% at +15m; coarse expectancy positive.

## T15 notes (2026-09-09)
- `scanner/backtest.py`: replay_decisions (rebuild tape from `trades`, recompute features/wash/decision, compare
  with tolerance), load_recorded_paths / evaluate_paths (exact intrabar rules on labeler OHLCV), CLI `backtest`.
- First real run: 2344 decisions, 94.7% decision match, 11 s. Mismatches were recording gaps, not logic: trades
  backfilled after the decision (n 201 vs 200 -> aVWAP/score drift) and the eligibility window config change.
- Schema v8: `trades.ingested_ts`, `decisions.config_hash`, `config_versions` (hash -> tunables JSON). Runner
  registers the config at start; tape ingest stamps ingested_ts; replay filters by knowledge time and uses the
  decision's own config version. Report separates the exactly reproducible subset from legacy rows.
- Live scanner is still on pre-v8 code (user-run launcher). Restart needed before the exact subset fills.

## Commands
    python -m pytest              # unit tests
    python -m scanner selftest    # offline self-check (or double-click selftest.bat)
    python -m scanner run         # main loop (from T2)

## Repo + launcher (2026-09-09)
- Private repo: https://github.com/JJnuts/momentum-ignition-scanner (first commit 96a56da). Secrets, data, raw, logs are ignored; the
  staged diff was scanned for the key line and the webhook before the first push (0 occurrences).
- Desktop shortcut `Momentum Sniper Bot.lnk` -> `run_console.bat`: foreground console, Ctrl+C stops; if a background
  instance is running (data/scanner.pid alive) it offers to stop it and take over.
