# Momentum Ignition Scanner — Build Roadmap

Created 2026-09-08. Companion to SPEC.md (design). One task per session.
Every task ships with: code, `tests/` (pytest), a live smoke check, and an
entry in STATUS.md (done / verified / open issues). Nothing is "done" until
its acceptance test passes in front of you.

Environment: Windows 11, Python 3.14, folder = this directory.
Secrets (Birdeye key, Discord webhooks, RPC URL) live in `.env` only.

Legend: [S] small (<1 session) · [M] one session · [L] one session + burn-in

---------------------------------------------------------------------------
## PHASE 0 — Foundation (free Birdeye tier is enough)

**T0  Skeleton** [S]
  - package layout `scanner/`, `requirements.txt`, `.env.example`, `config.json`
    (per-chain sections), logging (console + rotating file), SQLite init with
    full schema (scan_rows, candidates, trades, alerts, labels, safety,
    cu_ledger), `run.bat`, `STATUS.md`.
  - RAW RECORDER: every API response is appended gzip-jsonl to `raw/` with
    endpoint + params + timestamp. This makes every later task testable
    offline and is the seed of the replay backtester (T15).
  - Accept: `python -m scanner selftest` creates DB, writes a log line,
    loads config for both chains, fails loudly if `.env` is missing.

**T1  Birdeye client** [M]
  - auth header, per-plan token-bucket rate limiter, CU ledger (known costs
    per endpoint, logged per call and summed per day), retry/backoff,
    typed wrappers: token_list_v3, txs_token (recent + seek_by_time),
    ohlcv_v3, token_security, token_creation_info, trade_data/market_data,
    price_multiple.
  - Resolves the 3 open questions: exact trade-data endpoint + CU; do
    pre-migration pump.fun tokens appear in list v3; Robinhood chain slug.
  - Accept: live smoke on both chains against one known token each; each
    wrapper returns the expected fields; CU ledger matches call count;
    rate limiter provably holds at plan rps in a unit test.

---------------------------------------------------------------------------
## PHASE 1 — Wide scan, WATCH-only (buy Starter key before T2)

**T2  Stage 0 eligibility scan** [M]
  - per-chain list v3 calls with server-side filters (SPEC §3), normalized
    `TokenRow`, snapshot persisted per cycle, cadence 60 s SOL / 120 s RH,
    alternating sort keys.
  - Accept: 10 cycles run; no persisted row violates a filter; both chains
    populate; CU/day projection printed and ≤ budget (SPEC §2 table).

**T3  Stage 1 ignition features + nomination** [M]
  - rVol_1m/5m, price bands, participation counts, holder-growth (rolling
    cache), impact efficiency, percentile gates over the live universe,
    cohort z as ranker. Emits WATCH nominations with full feature vector.
  - Accept: unit tests on synthetic rows with known answers; replay over
    T2's recorded rows reports nominations/hour (target 5–30 SOL, 1–10 RH)
    — if far outside, thresholds are adjusted ONCE here, before freezing.

**T4  Labeler v1** [M]
  - for every nomination + a random control sample of eligible rows:
    snapshots at +5/+15/+30/+60 min (MFE, MAE, close, liquidity), cheapest
    endpoint. Persist to `labels`.
  - Accept: fake nomination → snapshots fire on schedule and persist;
    control sample is drawn; restart-safe (pending snapshots survive
    process restart).

**MILESTONE A — run WATCH-only for 3–5 days.** Review: nomination rate,
CU spend, labeler coverage, raw recorder size. No threshold changes.
  STARTED 2026-09-09 on Starter (detached process; see STATUS.md). While it
  runs, Phase 2 tasks (T5–T9) can be built and unit-tested offline; live
  checks against the running DB are read-only (WAL) — do not start a second
  `run` process (cycle ids and labels would race).
  NOTE for T7/T10: Birdeye holder-profile (25 CU, Standard+) and
  wallet-tags-tracker (30 CU, Lite+) are on Starter and Solana-only —
  use them as the Solana safety + smart-money-flow sources; RH stays on
  Blockscout. trade_data_single (10 CU) is a cheap Stage-2 confirmation
  source on Starter.

---------------------------------------------------------------------------
## PHASE 2 — Confirmation (the trade tape)

**T5  Trade tape** [M]
  - fetch last N trades per candidate, normalize (side, wallet, usd, ts,
    sig), in-memory ring per candidate, dedupe by signature, persist to
    `trades`. Poll 20–30 s for active candidates only.
  - Accept: live fetch on a hot token; ordering + dedupe verified; 5-min
    summed volume within ±15 % of the list-row volume_5m.

**T6  Trade-based features** [M]
  - activity windows (20/30/60 trades, max age), ignition-onset/anchor
    detection, OFI_30t, unique buyers/sellers, new-wallet share,
    trade-based aVWAP, 10-trade bars, higher lows, CLV.
  - Accept: synthetic tapes → exact expected values (all-buy tape OFI=+1,
    alternating=0, planted spike → correct anchor index); no lookahead
    (features at trade i use only trades ≤ i — asserted in test).

**T7  Wash score + vetoes** [M]
  - round-trip share, top-3 wallet share, size-uniformity flag, count-ratio
    trap, composite wash_score; concentrated-seller veto; rejection veto
    (needs 10-trade bars from T6).
  - Accept: planted wash tape scores ≥ 0.6; organic tape ≤ 0.3; planted
    dev-dump tape trips the seller veto with OFI still positive.

**T8  Candidate manager + budget guard** [M]
  - hysteresis enter/stay rules, max 12 candidates per chain with
    weakest-oldest eviction, deep-poll scheduler, daily CU cap that
    degrades to WATCH-only when hit (and pings you once).
  - Accept: simulated 30 nominations → cap + eviction order correct;
    budget guard trips in a test and recovers next day.

**T9  Scoring, tiering, decision timing** [M]
  - weighted score (SPEC §10), tiers (IGNITION ≥55, CONFIRMED ≥75),
    vetoes override, alert eligibility 30–90 s after anchor, evaluation at
    window close only, per-gate data-timestamp logging.
  - Accept: fixture feature vectors → expected tiers; a vector arriving at
    +20 s or +120 s after anchor is NOT alert-eligible; every gate row in
    the DB carries its data timestamp.

**T9b Pre-migration regime** [M] — only if T1 confirmed list v3 returns
  bonding-curve tokens. Progress velocity, buyers/min, tier cap, migration
  dedupe. Accept: synthetic progress series → velocity gate correct;
  pre→post same-mint continuity note instead of duplicate alert.

---------------------------------------------------------------------------
## PHASE 3 — Safety

**T10 Solana safety (RPC-based, plan-independent)** [M]
  - Birdeye token_security is Premium-only and creation_info is
    Business-only (verified 2026-09-08), so safety uses free Solana RPC
    directly: getAccountInfo on the mint (mint/freeze authority),
    getTokenLargestAccounts (top-20 holders → top-10 ex-LP/ex-curve),
    creator via pump.fun/PumpPortal metadata or Helius DAS free tier;
    LP burned via LP-mint supply/holders. Cached per token. Simple bundle
    heuristic (same-slot buys among top holders) marked OPTIONAL.
  - Accept: a known clean large token passes; a known frozen/mutable token
    fails; cache hit on second call (0 RPC calls); works with BIRDEYE plan
    set to "standard".

**T11 Robinhood safety (rebuild honeypot.py)** [L]
  - eth_simulateV1 buy→sell→transfer sim with balance-delta tax measurement
    (design preserved in `ROBINHOOD GEM FINDER DOCUMENTATION.txt`), owner
    renounced + verified source via Blockscout, top-10 via Blockscout.
  - Accept: a known honeypot on chain 4663 is rejected; a clean token
    passes with tax ≈ 0; simulation errors degrade to "UNKNOWN" not "SAFE".

**T12 Rug watch** [S]
  - scheduled re-checks at +10/+30/+60 after any ping; liquidity −40 % or
    safety flip → RUG WARNING to the same channel/thread.
  - Accept: fake alert + mutated liquidity → warning fires once, not
    repeatedly.

---------------------------------------------------------------------------
## PHASE 4 — Alerts

**T13 Discord alerts** [M]
  - webhook client with browser User-Agent (the 403 fix), embed card per
    SPEC §7, links (birdeye/dexscreener/fomo/axiom), 20-min per-token
    cooldown, 6/h/chain cap with CONFIRMED-only overflow, re-ping on tier
    upgrade only, daily heartbeat message ("alive, N nominations, CU used").
  - Accept: test card renders in a TEST channel; cooldown + cap unit
    tests; heartbeat fires.

**MILESTONE B — IGNITION pings ON. Thresholds FROZEN. Run until n ≥ 100
IGNITION+CONFIRMED alerts per chain.** No tuning inside the window.
  STARTED 2026-09-09 (test channel). MVP = T0–T13 complete.

---------------------------------------------------------------------------
## PHASE 5 — Analysis and reproducibility

**T14 tune.py** [M]
  - per-feature lift (winners vs control), MAE-before-MFE and time-to-peak
    distributions, rug rate, expectancy under the fixed rule (enter +60 s,
    stop at invalidation, time stop 15 m, trail −30 %), markdown report.
  - Accept: synthetic labeled dataset with planted patterns → tool ranks
    the planted features top; report renders.

**T15 Replay backtester** [L]
  - re-run Stage 1/2/3 deterministically over the raw recorder's stored
    responses; assert replay decisions == live decisions for the same
    period (reproducibility gate). This is the bridge to paper trading.
  - Accept: 24 h replay reproduces 100 % of live tiers/timestamps; runtime
    reported.

**MILESTONE C — first tuning pass from T14 → refreeze → second window.**

---------------------------------------------------------------------------
## PHASE 6 — v2 (after Milestone C; not scoped yet)

  T16 WebSocket rail (Premium): SUBSCRIBE_TXS for candidates, 15 s bars
  T17 Helius bundle/funder clustering; smart-wallet overlap feedback
  T18 Paper-trading executor (SPEC §12 rules, simulated fills w/ impact)
  T19 Live executor, minimum size, paper-vs-live slippage tracking

---------------------------------------------------------------------------
## Working agreement

- One task per session. I state the acceptance test up front, build, run
  the tests + live smoke, show you the output, update STATUS.md.
- Thresholds live only in config.json; code never hard-codes a number.
- Raw recorder is never disabled; disk is cheap, re-fetching is not.
- Use a Discord TEST channel until Milestone B.
- Small tasks make mistakes visible and cheap, not impossible. The live
  smoke after each task and the milestone burn-ins are the real guard.
