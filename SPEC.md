# Onchain Momentum Ignition Scanner — Design Spec v0.1

Status: DESIGN (2026-09-08). Signal-only. Pings Discord. No execution.
Chains: Solana + Robinhood Chain (chain id 4663). Data: Birdeye Data Services.
Predecessors: Solana bot_a / bot_b (2026-07-09, folder no longer on disk),
`ROBINHOOD GEM FINDER/rh_gem_finder.py` (2026-07-10; its alerts.py / honeypot.py
are missing from the folder and must be recreated).

---------------------------------------------------------------------------
## 0. Framing corrections (read first)

1. This is not a sniper. A sniper competes on latency at launch block. This
   system has 15–90 s of human latency (ping → read → open app → confirm).
   Its edge must come from SELECTION, not speed. Every threshold below is
   chosen so a signal that fires still has positive expectancy after ~60 s.

2. Measure fast, decide slow. Sample at 1 m bars / trade stream (or 15 s
   candles where available), but the DECISION horizon is 3–10 min and the
   intended hold is 10–45 min. First-spike detection is where the bots live;
   we hunt sustained participation growth, which is a different signal.

3. All features are dimensionless. A $20k token and a $5M token are judged
   by ratios (vol/liq, buy-share, unique buyers vs cohort), never absolute
   USD. Absolute floors exist only for tradability (slippage).

4. Volume-bot/wash resistance is half the alpha. Count-based "net buys" is
   the single most gamed metric on Solana. Everything is volume-weighted AND
   unique-wallet-weighted, with an explicit wash score.

5. Supply-structure/safety gates are not hygiene — on memecoins, ignition is
   frequently the dev/bundle exit. Safety is part of the signal.

6. New tokens have no history → time-series z-scores are undefined. Use
   cross-sectional normalization within (chain × age-bucket × mcap-bucket)
   cohort, and self-relative rVol from Birdeye's multi-window fields
   (volume_5m vs volume_1h/12) which needs no stored history.

7. Pre-migration (bonding curve) and post-migration (AMM pool) are two
   regimes with different features. Never pool their thresholds.

---------------------------------------------------------------------------
## 1. Architecture (funnel — the CU budget forces this shape)

```
STAGE 0  eligibility     Birdeye token_list v3, server-side filters      ~0.5 CU/token
STAGE 1  ignition        list-row features only (rVol, participation,    0 extra CU
                         price change bands, turnover sanity, cohort z)
STAGE 2  confirmation    per-candidate deep calls: trade-data (buy/sell  ~60–100 CU/eval
                         vol, unique wallets), recent txs (wash score),
                         OHLCV 1m (structure). Only for Stage-1 passers.
STAGE 3  safety          Solana: token_security (authorities, top10).
                         EVM: eth_simulateV1 honeypot/tax sim (own code)
                         + creation info. Cached per token.
STAGE 4  alert + label   tiered Discord card, cooldowns, auto-labeler
                         snapshots at +5/+15/+30/+60 min → SQLite.
```

One Python asyncio process. Modules:
`sources/birdeye.py` (REST; WS optional later) · `features.py` · `gates.py`
(config-driven, per chain, per regime) · `safety.py` · `alerts.py` (recreate;
must send browser User-Agent — Discord 403s the default aiohttp UA) ·
`labeler.py` · `tune.py` (port from RH bot) · `state.sqlite`.

Polling cadence: Solana Stage 0/1 every 60 s; Robinhood every 120 s (low
activity). Stage 2 only on Stage-1 passers, re-evaluated every 60 s while the
token stays in the candidate set (max 12 concurrent candidates per chain —
hard cap, oldest-weakest evicted).

---------------------------------------------------------------------------
## 2. Data — Birdeye facts that matter (verified 2026-09-08)

- Plans: Standard $0 / 30,000 CU/mo / 1 rps  (useless for this — ~600 list
  calls per month). Lite $39 / 2.5M CU / 15 rps. Starter $99 / 8M CU / 15 rps.
  Premium $199 / 20M CU / 50 rps / 500 WS connections.
- token_list v3: 50 CU per call, 100 rows max. Returns per token: liquidity,
  market_cap, fdv, holder, recent_listing_time, last_trade_unix_time,
  volume_{1m,5m,30m,1h,...}_usd, volume_*_change_percent,
  price_change_{1m,5m,30m,1h,...}_percent, trade_{1m,5m,30m,1h}_count.
  Server-side min_/max_ filters exist for all of these.
- OHLCV v3: 45 CU (<1000 candles). Recent txs: 12 CU (≤100 tx), 30 CU (≤300).
- Trade-data / market-data per token (buy vs sell volume, unique wallets per
  window): verify endpoint name + CU before coding (expected ~20–50 CU).
- Robinhood Chain: supported since 2026-07-09 (token_list v3 2026-07-13,
  token_security 2026-07-17, creation info 2026-08-07). Slug: verify in
  supported-networks page; WS docs mention "Robinhood" for 1s/15s/30s bars.
- Pre-migration pump.fun tokens: verify they appear in token_list v3 for
  Solana (Birdeye indexes pump.fun bonding-curve trades on the site; confirm
  the API returns them and that `liquidity` is meaningful pre-migration).
- ENDPOINT GATING BY PLAN (verified 2026-09-08):
    Standard (free): token_list v3, trending, OHLCV, recent txs. NO new
      listing, NO price multiple, NO seek_by_time, NO trade-data, NO
      security, NO creation info, NO holders, NO websockets.
    Lite/Starter: adds new listing, trade-data (single), market-data
      (single). Still NO security, NO creation info, NO websockets.
    Premium: adds token_security, seek_by_time, websockets.
    Business: adds creation info, holders, wallet endpoints.
  Consequences: Solana safety (§6) is built on free Solana RPC, not Birdeye
  (plan-independent). OFI/unique-wallet features are computed from the
  recent-txs tape (available on every plan), so trade-data is an
  optimization, not a dependency. Labeler uses list re-fetch/OHLCV, not
  price-multiple. The client carries a per-plan capability table and
  degrades gracefully.
- Plan staircase: Standard for T0–T4 dev + acceptance tests; Lite ($39,
  2.5M CU) for Milestone A at 90 s SOL / 180 s RH cadence (~2.2M CU/mo,
  WATCH-only); Starter ($99) from Phase 2 (deep calls); Premium only for
  the v2 WebSocket rail. Switching = one config value.

### CU budget (Starter, 8M/mo)
| item | calls/day | CU/day | CU/mo |
|---|---|---|---|
| Solana list, 60 s, 1 sort | 1,440 | 72k | 2.2M |
| Robinhood list, 120 s, 1 sort | 720 | 36k | 1.1M |
| Stage-2 deep evals @ ~100 CU | ≤1,200 | ≤120k | ≤3.6M |
| safety (cached, per new token) | ~300 | ~10k | 0.3M |
| **total** | | | **~7.2M** |

Starter fits if Stage 1 is selective (≤50 deep evals/hour across chains).
If it isn't, the fix is tighter Stage 1, not a bigger plan. Premium + WS
(SUBSCRIBE_PRICE 15 s/1 m bars, SUBSCRIBE_TXS for candidates) replaces the
Stage-2 polling entirely and is the v2 upgrade path.

---------------------------------------------------------------------------
## 3. Stage 0 — eligibility (server-side filters on token_list v3)

Solana (post-migration / AMM):
- min_liquidity 8,000 USD · min_market_cap 20,000 · min_holder 30
- min_last_trade_unix_time = now − 90 s (alive)
- min_volume_5m_usd 1,500 (cuts the tail; cheap)
- sort_by volume_5m_change_percent DESC (alt sort on even cycles:
  volume_1m_usd DESC) — one page of 100 each.

Solana (pre-migration / bonding curve) — separate call, separate regime:
- identify via creation-info / launchpad tag; mcap 20k–~70k (curve range)
- liquidity field may be curve SOL — treat separately; min_holder 25

Robinhood:
- min_liquidity 4,000 · min_market_cap 20,000 · min_holder 20
- min_volume_5m_usd 400 · sort_by volume_5m_change_percent DESC

Age: no hard floor beyond "has ≥3 one-minute bars of trades". Cohort
normalization handles youth. Max age: none — revival ignitions on old tokens
are valid (that was bot_b's job) but use the OLD cohort thresholds.

---------------------------------------------------------------------------
## 4. Stage 1 — ignition trigger (list-row features only)

Computed per row, per cycle. All must pass unless marked soft.

**Relative volume (self-relative, no history needed)**
- rVol_5m = volume_5m / (volume_1h / 12)   ≥ 3.0
- rVol_1m = volume_1m / (volume_30m / 30)  ≥ 4.0   (soft: ≥2.5 → WATCH)
- Both use log-space for the cohort z below; raw volume is heavy-tailed.

**Cross-sectional cohort z (bot_a logic)**
- feature f = ln(volume_5m / liquidity)
- cohort = chain × age bucket {<15m, 15m–2h, 2h–24h, >24h} × mcap bucket
  {20–100k, 100k–1M, >1M}; z computed over the current page + last 10 min of
  rows (rolling in-memory), need cohort n ≥ 12 else fallback to chain-wide z.
- z_f ≥ +1.5

**Participation (bot resistance, cheap version)**
- trade_5m_count ≥ 25 (Solana) / ≥ 10 (RH)
- trade_1m_count ≥ 8 / ≥ 3
- holder growth: holder now vs holder 5 min ago (rolling cache) ≥ +3 %

**Price band (ignition, not exhaustion)**
- price_change_5m in [+4 %, +60 %]
- price_change_1h ≤ +150 %  (do not buy the 4th leg)
- price_change_1m ≥ −8 %     (not mid-dump)

**Turnover sanity (wash pre-filter)**
- if volume_5m / liquidity > 2.0 and |price_change_5m| < 3 %  → WASH_SUSPECT, reject
  (huge turnover, no price impact = self-trading)

**Regime-specific (pre-migration)**
- progress velocity: Δ(curve progress %) per minute ≥ 2 %/min over last 5 min
- unique buyers/min rising 3 of last 5 minutes
- NOTE: migration itself is the catalyst AND the sell wave (early curve buyers
  dump into pool liquidity). Alert tier for pre-mig caps at IGNITION unless
  progress ≥ 85 % (migration imminent) with buyers still expanding.

Pass → token enters candidate set → Stage 2.

---------------------------------------------------------------------------
## 5. Stage 2 — confirmation (deep calls, per candidate, every 60 s)

Four blocks. CONFIRMED needs all four; IGNITION needs ≥2 incl. block A.

**A. Order-flow imbalance (the real signal — onchain taker flow IS OFI)**
- OFI_5m = (vBuy_5m − vSell_5m) / (vBuy_5m + vSell_5m)  ≥ +0.25
- OFI_1m ≥ +0.10 (not deteriorating into the alert)
- unique buyers_5m ≥ 15 (Sol) / ≥ 6 (RH); unique buyers ≥ 1.3 × unique sellers

**B. Wash / bot score (from last ≤300 txs)**
- roundtrip_share = volume from wallets that both bought AND sold inside the
  window / total volume                                  ≤ 30 %
- top3_wallet_share of window volume                     ≤ 50 %
- median trade size / mean trade size ≥ 0.25 (bots spray uniform micro-trades
  → ratio → 1.0 with tiny sizes; humans are lognormal) — treat as soft flag
  combined with count ratio: buys/sells count within 0.9–1.1 AND volume ≥
  1.5×liquidity → WASH
- new-wallet share (wallet first seen in this token in the window) ≥ 40 % —
  real ignition brings NEW participants; churn of the same 10 wallets doesn't.
- wash_score = weighted sum; reject if ≥ 0.6

**C. Structure (1m OHLCV, last 15 bars)**
- anchor = ignition bar (first bar where rVol_1m ≥ 4). aVWAP from anchor.
- close ≥ aVWAP
- CLV = (close − low)/(high − low): ≥ 0.5 on ≥ 2 of last 3 bars
- higher low: low[t] ≥ low[t−1] on ≥ 2 of last 3 bars
- rejection veto: on the highest-volume bar since anchor, upper wick ≤ 60 %
  of range (a huge-volume long-wick bar = distribution, not ignition)
- range expansion: (high−low)/close on ignition bar ≥ 2× median of prior 10
  (skip if <10 bars)

**D. Extension guard (latency-aware)**
- price now vs 15 m low ≤ +80 %  → else demote to WATCH ("late")
- bars since anchor ≤ 8         → else demote (the human is too late)

---------------------------------------------------------------------------
## 6. Stage 3 — safety (cached per token; re-check at +10/+30/+60 m)

Solana (Birdeye token_security + creation info):
- mint authority null, freeze authority null (hard)
- top10 holders (ex-LP/ex-curve) ≤ 35 % (hard); ≤ 20 % (bonus)
- creator holdings ≤ 10 % (hard ≤ 20 %)
- bundle heuristic: ≥ 5 of top-20 holders funded by same source / bought in
  the same slot → BUNDLE flag, cap at IGNITION tier. (Phase 2: Helius.)
- LP burned/locked for post-migration pools (soft; pump.fun migrations burn)

Robinhood (EVM):
- own eth_simulateV1 buy→sell→transfer sim; reject HONEYPOT or tax > 5 %
  (port from the missing honeypot.py; the design is in the RH docs txt)
- owner renounced (soft → demote if not), verified source (soft)
- top10 ≤ 35 % via Blockscout holders (free) or Birdeye holders

Post-alert rug watch (existing RH pattern): scheduled re-checks; if liquidity
drops > 60 % or a safety flag flips → RUG WARNING to the same thread.

---------------------------------------------------------------------------
## 7. Stage 4 — alert policy & card

Tiers: WATCH (Stage 1 only, no ping — log only) · IGNITION (ping) ·
CONFIRMED (ping, highlighted). Re-ping only on tier upgrade.
Cooldown: 20 min per token. Global cap: 6 pings/hour/chain; when the cap is
hit, only CONFIRMED goes through. Dedup across regimes on migration (same
mint pre→post gets one continuity note, not a fresh alert).

Card fields:
```
[CONFIRMED] SOL · $TICKER · mcap 84k · liq 21k · age 11m · holders 212 (+9%/5m)
rVol5 6.1x · z 2.3 · OFI5 +0.41 · uniq buyers 38 vs sellers 14 · new-wallet 57%
wash 0.12 · top10 18% · creator 3% · mint/freeze ✓ · LP burned ✓
price 0.000084 · +23% 5m · +31% from anchor · bars since anchor 4
INVALIDATION 0.000067 (anchor low / aVWAP, −20%)   TIME STOP 15m no new high
SIZE NOTE: ≤1% of liq per clip (~$200) to keep slippage <3%
links: birdeye · dexscreener · fomo · axiom
```
Invalidation = max(anchor-bar low, aVWAP) but never wider than −25 %.
No fixed take-profit: memecoin momentum is right-skewed; the card gives a
time stop and the trader trails. (Labeler will tell us whether a fixed
1.5R partial improves realized expectancy — measure, don't assume.)

---------------------------------------------------------------------------
## 8. Labeler & validation (build this FIRST; it's the whole project)

At every alert (all tiers incl. WATCH) store the full feature vector +
price/liq. Snapshot at +5, +15, +30, +60 min: high (MFE), low (MAE), close,
liquidity, safety flags. Also label a random sample of Stage-0 rows that
did NOT trigger (control group — without it, lift is unmeasurable).

Outcome metrics per tier/chain/regime:
- hit rate of MFE ≥ +30 % within 15 m; MFE ≥ +50 % within 30 m
- median MAE before MFE (tells you where the stop belongs)
- rug rate (liq −60 % or safety flip) within 60 m
- time-to-peak distribution (tells you the time stop)
- expectancy under a fixed rule: enter at alert+60 s, stop at invalidation,
  exit at time stop or trailing −15 % from peak.

Protocol (same as the shadow monitors): thresholds FROZEN at v0.1 values
until n ≥ 100 IGNITION+CONFIRMED alerts per chain. Then one tuning pass via
tune.py (lift ranking per feature), re-freeze, repeat. No intra-window
tweaking. Expect a realistic win rate of 30–40 % with the P&L in the tail;
judge on expectancy and MAE/MFE shape, not hit rate.

Offline pre-check (optional, before going live): Birdeye OHLCV + txs history
allow a replay backtest on Solana. Universe must come from historical
new-listing lists at time T (not today's survivors) or the test is
survivorship-biased and worthless.

---------------------------------------------------------------------------
## 9. Build order

1. `sources/birdeye.py` + config + Stage 0/1 on both chains, WATCH-only,
   logging every candidate + labeler snapshots. Run 3–5 days. (Starter plan.)
2. Stage 2 (OFI, wash, structure) + Stage 3 safety (recreate honeypot sim
   from RH docs; Birdeye token_security for Solana). Turn on IGNITION pings.
3. tune.py port; first frozen window to n=100.
4. v2: Premium + WebSocket trade stream for candidates; Helius bundle
   detection; pre-migration module hardened; optional third EVM chain.

Non-goals for v1: execution, wallet forensics beyond top-holder checks,
any signal with a horizon under 2 minutes.

---------------------------------------------------------------------------
## 10. v0.2 gate refinements (2026-09-08, supersede §4–§5 where they conflict)

**Composition rule.** Do NOT AND together 10+ gates. If 8 gates each pass
85 % of true ignitions, only 27 % of real winners survive. Structure:
  HARD VETOES (few, binary)  +  IGNITION EVENT (mandatory, defines anchor)
  +  CONFIRMATION SCORE (weighted, 0–100)  →  tier by score.
  IGNITION tier ≥ 55, CONFIRMED ≥ 75. Vetoes always win.

**Hysteresis.** Enter candidate set on the full trigger; STAY in it on a
weaker condition (rVol_5m ≥ 2, OFI_5m ≥ 0) for up to 10 min. Prevents
flapping and lets bar-2/3 confirmation happen.

**Percentile gates at cold start.** Until the labeler has data, express
rVol / participation thresholds as percentiles of the live eligible
universe per chain (e.g. rVol_1m in top 5 %, unique buyers_5m in top 10 %)
rather than fixed numbers. Self-calibrates across chains/regimes/market
temperature. Convert to fixed numbers after the first tuning window.

**Cohort z is a RANKER, not a gate** (n too small on Robinhood; noisy on
Solana intraday). Use it to order candidates for the deep-call budget.

**New feature: impact efficiency.**
  eff_5m = price_change_5m / (volume_5m / liquidity)
Real ignition on a thin book has HIGH efficiency; efficiency collapsing
while volume stays high = supply appearing (distribution) or wash.
Gate: eff_5m ≥ chain-percentile 40; veto if eff_5m < 0 with rVol_5m ≥ 3.
Replaces the crude turnover-sanity rule in §4.

**New veto: concentrated seller.** From recent txs: if top-3 seller
wallets ≥ 60 % of sell volume in the window AND any of them holds
≥ 3 % supply → DISTRIBUTION veto (dev/bundle exit signature), even if
OFI is still positive. This is the single most important anti-gate.

**Alert timing window.** Alert is eligible at bars 2–5 after the anchor
bar (bar 1 = unconfirmed, bar ≥ 6 = human is late). Emit at bar-close
only, never on partial bars — this keeps the signal reproducible for the
later backtest/paper-trade phase.

**Confirmation score weights (prior; tune by lift):**
  participation breadth (unique buyers, buyer/seller ratio, new-wallet share) 30
  OFI_5m level + OFI_1m non-deteriorating                                     25
  impact efficiency                                                           15
  structure (aVWAP, CLV, higher lows)                                         15
  holder growth rate 5m                                                       10
  safety bonus (top10 ≤ 20 %, creator ≤ 3 %, LP burned)                        5

**Reproducibility for execution phase.** Log, per gate, the evaluation
timestamp and the data timestamp it used. Gates must be a pure function of
(data available at bar close). No partial-bar peeking, no "latest price"
mixed with closed-bar features.

---------------------------------------------------------------------------
## 11. Clock design (2026-09-08) — replaces "1m bars" in §4–§5

Three clocks, not one:
- SAMPLING clock: as fast as the data rail allows. WS trade stream / 15 s
  candles (Premium) or REST txs polling every 20–30 s for candidates only
  (Starter). The 1 m figure in §1 was a Starter-budget artefact for the
  wide scan, not a signal-design choice.
- FEATURE clock: ACTIVITY-BASED, not time-based. Time bars are the wrong
  primitive on-chain: a 15 s bar on a $50k token holds 0–5 trades (noise),
  a 1 m bar on a hot token holds 300 (lag). Define windows in TRADES:
    ignition window   = last 20 trades (max age 90 s)
    OFI window        = last 30 trades
    wash window       = last 60 trades
    structure         = trade-based aVWAP + higher lows on 10-trade bars
  Stage 2 needs NO candles: the trade tape from the txs endpoint is the
  primitive. OHLCV is used only for the extension guard (15 m low) and the
  labeler.
- DECISION clock: signaling phase → alert eligible 30–90 s after ignition
  onset (was bars 2–5 of 1 m = 2–5 min). Execution phase → sub-30 s,
  same features, same code, WS rail.
Ignition onset (anchor) = first trade after which the 20-trade window's
volume rate ≥ 4× the trailing 30 m rate AND ≥ 8 distinct buyers.

Wide scan (Stage 0/1) stays on token_list v3 at 60 s / 120 s; its job is
only to nominate candidates, and its 1 m/5 m fields are adequate for that.

---------------------------------------------------------------------------
## 12. Risk management (signaling phase = card + human; execution = code)

Position level
- Size by EXIT liquidity, not conviction: clip ≤ 1 % of pool liquidity and
  assume liquidity at exit = 50 % of entry. Round-trip impact ≤ 5 %.
- Fixed-fractional risk: R = 0.5–1 % of bankroll. Position = R ÷ stop
  distance, then min() with the liquidity cap. Never override the min.
- Stop: max(anchor low, aVWAP), capped at −25 %. The LABELER's MAE-before-
  MFE distribution sets the real number; −25 % is a placeholder.
- Time stop: no new high within 15 min of entry → exit. Non-negotiable in
  momentum: a move that doesn't continue has already failed.
- Exit ladder (prior, to be tested vs pure trail): 50 % off at +100 %,
  remainder trails −30 % from peak or aVWAP loss, whichever first.
- Rug exit: liquidity −40 % from entry, safety flag flip, creator/top-holder
  sell ≥ 2 % supply → exit immediately, no confirmation.

Portfolio level
- Max 3 concurrent positions; total memecoin exposure ≤ 25 % of bankroll.
- Daily loss limit −3 R → stop for the day. 4 consecutive stops → 2 h pause
  (regime tell, not bad luck).
- Cluster cap: max 2 positions in the same launchpad/meta at once.
- Regime gate: SOL −3 % in 1 h, or eligible-universe ignition count in the
  bottom decile of its trailing 7 d → half size or stand down. Tie into the
  existing regime monitor.

Phase gating (paper → live)
- Live only at minimum size until n ≥ 50 live trades reproduce paper
  expectancy within tolerance. Track paper-vs-live slippage per trade; a
  widening gap is the failure mode that kills these systems.
- Dedicated trading wallet holding only the bankroll. Never the main wallet.
- Execution phase adds: slippage tolerance ≤ 3 %, Jito bundle / priority
  fee policy, failed-tx retry cap 2, partial-fill handling, sandwich
  exposure check on entry size vs pool depth.

---------------------------------------------------------------------------
## 13. Live findings from the T1 smoke (2026-09-08, free plan, 300 CU)

1. Solana token_list v3 returns every field the design needs (1m/5m/30m/1h
   volume, price change, trade counts, holder, listing time). Stage 1 on
   Solana can run from list rows alone, as designed.

2. ROBINHOOD token_list v3 has NO sub-1h fields: volume/price-change/trade
   counts exist only for 1h, 2h, 4h, 8h, 24h (plus buy_24h/sell_24h and
   unique_wallet_24h). Consequence: Robinhood Stage 1 cannot compute rVol_1m
   or rVol_5m from the list. Options for T3: (a) nominate from 1h fields and
   Δ(volume_1h_usd)/Δ(trade_1h_count) between consecutive 120 s polls, then
   build 1m/5m features from the trade tape (txs v3, 12 CU) for the top-N
   only; (b) OHLCV 1m (45 CU) per candidate. Prefer (a). Re-verify on an
   active RH token in T2 in case the fields are omitted only when null.

3. Pre-migration launchpad tokens DO appear in Solana list v3: a
   `raydium_launchlab` (Bonk) bonding-curve token showed with liquidity=0,
   and post-migration PumpSwap tokens show `source=pump_amm`. Consequence:
   the Stage-0 `min_liquidity` filter EXCLUDES bonding-curve tokens, so the
   pre-migration regime needs its own list query without a liquidity floor
   (mcap band + holder floor instead), exactly as §3 anticipated. Trade
   `source` values seen: pump_amm, raydium_launchlab, uniswapV4 (RH).
   Expect `pump_dot_fun` for pump.fun curve trades (not sampled).

4. Free-tier limiter is strict: at exactly 1 rps, 5 of 15 calls got 429.
   Client now runs at rate_safety (0.8) x plan rps with no burst on the
   1 rps tier. Birdeye returns x-ratelimit-limit/remaining/reset headers
   (limit=100 observed) — a second, longer-window limiter; the client logs
   them and can adapt later.

5. CU ledger matched the verified per-call table exactly (150+90+60 = 300).
   Compare against the Birdeye dashboard once to confirm 429s aren't billed.

6. (T2, 2026-09-08) Robinhood list v3 returns `holder: null` for EVERY token,
   so any `min_holder` server filter empties the chain. Robinhood Stage 0
   therefore filters on liquidity / market cap / volume_1h only; holder
   counts for Robinhood come from Blockscout in Stage 3. Confirmed over a
   full 100-row page that vol_5m/1m/30m are absent on Robinhood (0/100),
   not merely null-omitted. Solana list: 100 rows, 0 client-side filter
   violations, i.e. Birdeye's server-side filters are trustworthy.
   `PONS` (Pons launchpad token) appears among top-liquidity RH tokens.

7. (T2) Free-tier limiter: a 429 still occurred at ~1.3 s spacing between
   two chains' first calls; rate_safety lowered to 0.5 (2 s spacing) on
   the free tier, no 429s since. Irrelevant on paid tiers (15+ rps).

---------------------------------------------------------------------------
## 14. Stage 1 calibration record (T3, 2026-09-08) - thresholds now FROZEN

Data: 18 Solana cycles / 9 Robinhood cycles (~0.33 h) of stored Stage-0 rows,
replayed with no lookahead. Roadmap allowed ONE adjustment before freezing.

Solana (short mode), before -> after:
- rvol_1m_min 4.0 -> 1.5. Finding: rVol_1m >= 4 only fires on the FIRST minute
  of a burst (78% of rows fail it), which contradicts the bars-2-5 decision
  window. rVol_1m is now a RECENCY check ("still going"); rVol_5m >= 3 is the
  ignition gate. Baseline definition improved vs SPEC s4: baseline EXCLUDES
  the current window and shrinks to the token's age
  (base_5m = (vol_1h - vol_5m) / prior_units).
- page-percentile gates (rvol_1m top 5%, trade_5m top 10%) -> disabled.
  Finding: the top-movers page mixes SOL-sized tokens with 50k-cap tokens,
  so "top 10% by trade count" is unreachable for any small igniter (0 passes
  in 1800 rows). Percentiles are only meaningful within a cohort; impact-
  efficiency percentile (dimensionless) is kept at >= 40.
- Result: 23.5 nominations/h (target 5-30). Nominees looked like ignitions:
  rVol_5m 3-19x, +5..32% on 5m, eff pct 65-96.

Robinhood (hourly mode), before -> after:
- Finding: igniters ENTER the top-100 page, so most have no previous poll and
  hit the fallback path; the delta path (rvol_dt) works once a token is on
  two consecutive pages (RSTR 4.5x, INJOH 13.6x with +127/+132 trades).
- fallback: vol_1h_chg >= 200% -> 2000%, trade_1h >= 20 -> 50,
  pc_1h >= 4% -> 10%, new vol_1h floor 5000 USD, new extension cap for
  entrants pc_1h <= 80% (entrants at +100..134% are late, not igniting).
- delta path: rvol_dt >= 3 -> 4, d_trades >= 5 -> 8.
- Result: ~26/h in a cold-start replay (every existing mover nominated at
  once); steady state expected well below. RH Stage 1 is a coarse pre-filter
  for tape polling; the real RH ignition signal arrives with T5/T6.
  Re-check at Milestone A against the 1-10/h target; do not tune before.

Frozen until n >= 100 IGNITION+CONFIRMED alerts per chain (SPEC s8).

---------------------------------------------------------------------------
## 15. Labeler v1 design record (T4, 2026-09-08)

Two passes, chosen for cost:
- CLOSE pass at every horizon (+5/+15/+30/+60): price + liquidity from a
  scan_rows snapshot within +-90 s (free; the igniter usually stays on the
  page) else one multi_price batch per chain per tick (Lite+, ceil(3n^0.8)
  CU for n addresses). Retries until 600 s after due, then FAILED.
- PATH pass at the final horizon, NOMINATIONS ONLY: one OHLCV 1m call over
  [t0, t0+60m] (45 CU) fills high/low for all horizons -> MFE/MAE. Controls
  get close-based labels only. Treatment-vs-control lift uses close returns
  (both groups, unbiased); stop/target calibration uses MFE/MAE (treatment).
- CONTROL sample: 0.5 expected draws per cycle from eligible, non-nominated
  rows, seeded RNG (replayable), stored as tier=CONTROL nominations WITH
  their feature vectors.
- Cost at Starter cadence: ~45 CU x (27 SOL + 20 RH)/h ~ 51k CU/day for
  paths + ~9k/day for batches ~ 1.8M CU/mo. Fits Starter with Stage 0
  (3.3M) and leaves ~2.9M for Stage 2. Too much for Lite alongside Stage 0;
  Milestone A on Lite should set ohlcv_path_for="none" or a lower
  control rate if budget binds.
- Restart-safe: pending work lives in `labels` (status/path_status/attempts).
- First real data point: AMC Stage-1 ignition 14:25 -> +9.3%/+32.7%/+65.9%
  close at +5/+15/+30 min, MAE +1.4%. n=1; means nothing yet.

---------------------------------------------------------------------------
## 16. Trade tape findings (T5, 2026-09-09, Starter)

- One transaction can carry several trade legs (8/20 tx hashes duplicated on a
  Robinhood page). Dedupe key = tx_hash : ins_index : inner_ins_index (Solana)
  or log_index (EVM). Pages overlap when trades arrive between page calls
  (16-18 dups per 300 on hot tokens) - harmless with signature dedupe.
- The token's own leg is whichever of from/to matches the token address; its
  `price` is the token USD price and `ui_amount` the token quantity. `side`
  and `volume_usd` are top-level.
- Verification against Birdeye's own trade-data (same instant): memecoin-class
  tokens agree within ~1% USD and ~3-6% trade count (ZCAT $45.6k vs $45.5k;
  NEAR $26.8k vs $27.1k). The LIST row lags more than its timestamp implies
  (ZCAT list $38.5k at "age 5 s"), so the acceptance reference is trade-data,
  not the list. Low-activity multi-venue tokens (TRX, jlUSDC) show large gaps
  (tape 23 vs 35 trades) with the newest tape trade 40 s old -> see the
  tx_type / lag check below.
- SOL/USDC-class tokens trade hundreds of times per second: 300 trades = 1 s.
  The tape is only meaningful for candidates, never for majors; the tape-check
  picker now targets 20-200 trades per 5 min.
- Cost model as built: first fetch 2 pages (24 CU), then 1 page every 3rd
  poll at 60 s (12 CU / 3 min) while a candidate stays active (8 min) ->
  ~48-60 CU per candidate. Daily tape budget 80k CU, enforced in the poller
  in addition to the global daily cap.
- Follow-up on the TRX gap: sampled at the SAME instant, trade-data 17 trades /
  $5,457 vs tape 15 / $5,455 (tx_type swap == all). The earlier 23-vs-35 gap
  was indexing lag: the txs endpoint trails the stats endpoint by 40-80 s on
  quiet tokens, ~2 s on hot ones. Stage 2 evaluates hot candidates, so the
  practical lag is seconds; T6 should still timestamp features by the tape's
  newest trade, not wall clock.

---------------------------------------------------------------------------
## 17. Trade-based features (T6, 2026-09-09)

Implemented in `scanner/features.py` as pure functions of trades with
ts <= as_of (as_of = tape's newest trade, never wall clock). Invariant tested:
computing on a prefix == computing on the full tape as of that time, at
timestamp boundaries (several trades share one second on Solana).
- Windows are trade counts with a max age: ignition 20 (<=90 s), OFI 30,
  recent 10, wash 60 (<=600 s), min 8 trades else undefined. OFI is USD-
  weighted taker imbalance (9 dust buys + 1 big sell -> negative), plus buy
  share, unique buyers/sellers, buyer:seller ratio, new-wallet share (buy USD
  from wallets NOT seen earlier in the tape), trades per wallet, USD rate.
- Anchor (ignition onset): earliest i within a 900 s lookback whose 20-trade
  window USD rate >= 4x the trailing-30-min rate (baseline EXCLUDES the
  window, needs >= 120 s of history) with >= 8 distinct buyers. A single
  wallet spraying volume never anchors. Onset is stable as more trades arrive.
- Structure since the onset window: exact anchored VWAP (sum usd / sum
  amount), 10-trade bars, CLV, higher lows 2-of-3, rejection = upper wick
  fraction of the highest-USD bar, price vs anchor / vs aVWAP, max since
  anchor. Partial last bar kept only if >= half a bar.
- Persisted per poll to `tape_features` (schema v4) with a features_json
  blob: inputs for T9 scoring, T14 tune.py and T15 replay.
- On real tapes: tokens whose 200-trade tape spans ~2 min have NO trailing
  history -> anchor undefined. T8 must fetch deeper on first contact for hot
  tokens (or fall back to the Stage-1 ignition time as the anchor).
  new_wallet_share = 0 on such tapes flags wallet churn (bot-like), a useful
  wash input for T7.

---------------------------------------------------------------------------
## 18. Wash score and vetoes (T7, 2026-09-09)

`scanner/wash.py` (pure) + `scanner/enrichment.py` (cached Birdeye lookups).
- Composite wash_score over the last 60 trades: roundtrip share (wallets that
  both bought and sold) 0.35, top-3 wallet share 0.25, size uniformity
  (median/mean) 0.15, count trap (buy:sell count ~1 with turnover >= 1.5x
  liquidity) 0.15, churn (trades per wallet, low new-wallet share) 0.10; each
  normalised lo->hi to 0..1. WASH veto at >= 0.60. On real tapes: a textbook
  wash (Percolator: roundtrip 100%, top-3 94%, 3.5 trades/wallet, 5% new)
  scored 0.68; organic tokens 0.08-0.35. Size uniformity is tiny on Solana
  memecoins (heavy right tail), so that component rarely fires.
- DISTRIBUTION veto: top-3 sellers >= 60% of sell USD AND one holds >= 3% of
  supply (holdings from token_top_traders holdVolume / supply, supply =
  market_cap/price from the scan row). Holdings unknown -> soft
  DISTRIBUTION_SUSPECT only when share >= 85% (common on quiet tokens with
  few sellers, hence soft).
- REJECTION veto: upper wick > 60% of range on the highest-USD bar since anchor.
- Tag-flow vetoes from Birdeye wallet-tags-tracker (Solana, 30 CU, 5-min
  buckets, tags dev/sniper/smart_trader/kol ONLY - bundler/insider are not
  offered by this endpoint and will come from holder-profile in T10):
  DEV_INSIDER_SELLING (>= 20% of window sell USD), BUNDLER_SELLING (>= 40%).
  smart_trader net flow is kept as a score input for T9. Probe on a busy
  token returned kol + smart_trader buckets with buy/sell USD and wallet
  counts; the default 1D time_frame returns empty groups - always pass
  time_frame=5m and explicit tags.
- Enrichments cached 5 min per candidate in `enrichment`, budgeted (40k
  CU/day) on top of the global cap. Persisted per snapshot: wash_score,
  hard_vetoes, soft_flags, wash_json (schema v5).

---------------------------------------------------------------------------
## 19. Candidate manager (T8, 2026-09-09)

`scanner/candidates.py`, persisted in `candidates` (restart resumes the set).
- ENTER on a Stage-1 nomination; a repeat nomination refreshes (one entry).
- STAY: evidence from either a Stage-1 page row (rVol_5m / rvol_dt >= 2.0)
  or the tape (OFI30 >= 0.0) refreshes last_seen. A failed stay check only
  counts; silence for max_stay_min (8) expires the candidate.
- CAP 12 per chain: a stronger newcomer evicts the weakest (strength = Stage-1
  rVol x (1 + OFI30), floored at 0.1x), ties -> the OLDEST goes. Weaker
  newcomers are rejected.
- VETO: any hard veto from Stage 2 removes the token and blocks re-entry for
  20 min. DEGRADE: when the day's CU (by the manager's own clock) reaches
  the cap, active() is empty -> no deep polls, Stage 0/1 keep running
  (WATCH-only) until the day rolls over.
- First-contact depth (T6 finding): the poller keeps paging on first
  contact until the tape spans >= 600 s (cap 6 pages = 72 CU) so the anchor
  has a trailing baseline; quiet tokens stop after the initial 2 pages.
- Replaces the provisional "recent WATCH nominations" active set.

---------------------------------------------------------------------------
## 20. Scoring, tiers and decision timing (T9, 2026-09-09)

`scanner/scoring.py`; decisions persisted to `decisions` (schema v6), one per
candidate per poll, with per-component points, inputs and the DATA timestamp
each component used (tape as_of for tape components, Stage-1 nomination ts
for efficiency / holder growth). T13 reads `alertable` rows.
- Weights (SPEC s10): participation 30 (unique buyers 5->15, buyer:seller
  1.0->1.3, new-wallet share 0.15->0.40, averaged), order flow 25 (OFI30
  0->0.5 for 20 + 5 if OFI10 >= 0.10), efficiency 15 (Stage-1 eff pct
  40->100), structure 15 (price >= aVWAP 5, CLV 2-of-3 5, higher lows 5),
  holder growth 10 (0->3% vs the 5-min-ago page row; often unavailable for
  page entrants -> 0), safety 5 (T10).
- Tiers CONFIRMED >= 75, IGNITION >= 55, else WATCH; any hard veto -> VETO.
- Anchor = tape onset if found, else the Stage-1 nomination time (source
  recorded). since_anchor uses the tape's newest trade, not wall clock.
  Eligibility window widened from [30, 90] to [30, 180] s because the tape
  polls every 60 s; a 60-s window would be hit by at most one poll.
- Offline check on recent candidates: BRA 76 CONFIRMED (late, 405 s),
  XBT 75 at 61 s and BUG 74 at 52 s (eligible -> would alert), OTC 44 WATCH
  (participation 30 but OFI 0: buyers without aggression), three VETO rows.
  Holder growth was 0 for every candidate (page entrants have no prior
  snapshot) - a known weakness of that component, worth revisiting at
  Milestone C.
