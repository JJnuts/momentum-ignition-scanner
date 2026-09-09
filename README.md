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
