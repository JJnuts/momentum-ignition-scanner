# Momentum Ignition Scanner

Signal-only onchain momentum scanner for Solana and Robinhood Chain.
Scans Birdeye token lists, detects ignition (relative volume, participation,
price response, impact efficiency), nominates candidates, and labels their
forward outcomes so thresholds can be calibrated on evidence.

Design: `SPEC.md`. Build order and acceptance tests: `ROADMAP.md`. Task ledger: `STATUS.md`.

## Setup

```
pip install -r requirements.txt
copy .env.example .env      # or create secrets.txt
```

Put your keys in `.env` (or `secrets.txt`, `secrets.env`, `env.txt`):

```
BIRDEYE_API_KEY=...
DISCORD_WEBHOOK_TEST=...
```

Secrets files are git-ignored. Never commit them.

## Run

```
python -m scanner selftest        # offline self-check
python -m scanner smoke           # live Birdeye check (~300 CU)
python -m scanner run             # main loop (Ctrl+C to stop)
python -m scanner run --once      # one cycle per chain
python -m scanner replay          # re-evaluate Stage 1 over stored rows (no API calls)
python -m pytest                  # unit tests
```

Windows helpers: `run_console.bat` (foreground, Ctrl+C stops), `start_scanner.bat`
(detached), `stop_scanner.bat`.

## Layout

```
scanner/
  config.py      config.json + secrets loading
  plans.py       Birdeye plan gating, endpoint paths, CU costs
  birdeye.py     API client: gating -> rate limit -> HTTP -> raw record -> CU ledger -> retry
  ratelimit.py   token bucket
  ledger.py      compute-unit ledger (SQLite)
  recorder.py    raw response recorder (gzip JSONL, feeds offline tests + replay)
  db.py          schema + migrations
  stage0.py      eligibility scan (token list v3)
  stage1.py      ignition features + WATCH nominations
  labeler.py     forward outcome labels (+5/+15/+30/+60 min) with control samples
  replay.py      Stage 1 replay over stored rows
  runner.py      asyncio main loop
tests/           pytest (offline; real API shapes in tests/fixtures)
```

All thresholds live in `config.json` and are frozen between calibration windows.

## Roadmap status

MVP complete (T0–T13, 2026-09-09): scan → nominate → trade tape → features → wash/vetoes →
candidates → score → safety (Solana + Robinhood) → rug watch → Discord alerts. Milestone B
(burn-in with alerts on, thresholds frozen) is running.

Remaining, in order (details in `ROADMAP.md`):

| task | what it delivers | status |
|---|---|---|
| Milestone B | run until ≥100 IGNITION+CONFIRMED alerts per chain | done (505 Solana / 225 Robinhood) |
| T14 tune.py | per-feature lift vs control, MAE/MFE and time-to-peak distributions, expectancy under the fixed rule | done |
| T15 replay backtester | deterministic re-run of every stage over recorded data; must reproduce live decisions (knowledge time + config version, schema v8) | done |
| Milestone C | first evidence-based tuning pass, refreeze, second window | done 2026-09-14 (Solana IGNITION bar 60; FROZEN v2) |
| T15a–T15d | CU budget: local-day cap, quiet window 09–15 local, spend trims, live verification | a–c done, d running |
| Milestone D | second window ≥100 alerts per chain, second tuning pass | running |
| T9b pre-migration regime | bonding-curve tokens (progress velocity, buyers/min, migration dedupe) | optional |
| v2: T16–T19 | WebSocket rail (Premium), Helius bundle clustering, paper executor, live executor at minimum size | not scoped |
