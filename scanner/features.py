"""Trade-based (Stage 2) features - pure functions over a token's tape.

Clock design (SPEC s11): windows are ACTIVITY-based (trade counts with a max
age), not time bars. `as_of` defaults to the tape's newest trade, never the
wall clock. Everything is a pure function of trades with ts <= as_of, so
computing on a prefix equals computing on the full tape "as of" that time
(no lookahead) - asserted in tests.

Feature families:
  windows    OFI (USD-weighted taker imbalance), buy share, unique buyers /
             sellers, new-wallet share, over the last N trades.
  anchor     ignition onset: first trade after which the 20-trade window's
             USD rate >= k x the trailing rate (baseline EXCLUDES the window)
             with >= m distinct buyers; searched within a lookback.
  structure  anchored VWAP (exact: sum usd / sum amount), 10-trade bars since
             the anchor, CLV, higher lows, rejection (upper wick on the
             highest-volume bar), extension since anchor.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .tape import Trade

DEFAULTS: dict[str, Any] = {
    "ignition_window_trades": 20,
    "ignition_max_age_s": 90,
    "ofi_window_trades": 30,
    "recent_window_trades": 10,
    "wash_window_trades": 60,
    "window_max_age_s": 600,
    "min_window_trades": 8,
    "anchor_rate_mult": 4.0,
    "anchor_min_buyers": 8,
    "anchor_trailing_s": 1800,
    "anchor_min_trailing_s": 120,
    "anchor_lookback_s": 900,
    "bar_trades": 10,
    "max_bars": 30,
}


@dataclass
class Bar:
    i_start: int
    i_end: int
    ts_start: int
    ts_end: int
    o: float
    h: float
    l: float
    c: float
    usd: float
    n: int

    @property
    def rng(self) -> float:
        return self.h - self.l

    @property
    def clv(self) -> float | None:
        return (self.c - self.l) / self.rng if self.rng > 0 else None

    @property
    def upper_wick_frac(self) -> float | None:
        return (self.h - max(self.o, self.c)) / self.rng if self.rng > 0 else None


@dataclass
class WindowStats:
    n: int = 0
    span_s: int = 0
    buy_usd: float = 0.0
    sell_usd: float = 0.0
    ofi: float | None = None            # (buy - sell) / (buy + sell)
    buy_share: float | None = None
    buyers: int = 0
    sellers: int = 0
    buyer_seller_ratio: float | None = None
    new_wallet_share_usd: float | None = None   # buy USD from wallets first seen in this window
    new_wallets: int = 0
    trades_per_wallet: float | None = None
    usd_rate: float | None = None       # USD per second over the window

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TapeFeatures:
    n: int
    as_of: int | None
    oldest_ts: int | None
    newest_ts: int | None
    price: float | None = None
    ignition: WindowStats = field(default_factory=WindowStats)
    ofi30: WindowStats = field(default_factory=WindowStats)
    recent: WindowStats = field(default_factory=WindowStats)
    wash: WindowStats = field(default_factory=WindowStats)
    # anchor
    anchor_idx: int | None = None
    anchor_ts: int | None = None
    anchor_price: float | None = None
    anchor_rate_ratio: float | None = None
    trades_since_anchor: int | None = None
    seconds_since_anchor: int | None = None
    # structure
    avwap: float | None = None
    price_vs_avwap_pct: float | None = None
    price_vs_anchor_pct: float | None = None
    anchor_low: float | None = None
    max_price_since_anchor: float | None = None
    bars_n: int = 0
    clv_last: float | None = None
    clv_ge_half_2of3: bool | None = None
    higher_lows_2of3: bool | None = None
    rejection_wick_frac: float | None = None   # upper wick of the highest-USD bar since anchor
    ofi_recent_nondeteriorating: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def _settings(cfg: dict[str, Any] | None) -> dict[str, Any]:
    s = dict(DEFAULTS)
    if cfg:
        s.update({k: v for k, v in cfg.items() if not k.startswith("_")})
    return s


def window_stats(trades: list[Trade], start_idx: int, end_idx: int, prior_wallets: set[str],
                 min_n: int) -> WindowStats:
    """Stats over trades[start_idx:end_idx] (end exclusive). prior_wallets = wallets seen before start_idx."""
    w = trades[start_idx:end_idx]
    st = WindowStats(n=len(w))
    if not w:
        return st
    st.span_s = w[-1].ts - w[0].ts
    buyers: set[str] = set()
    sellers: set[str] = set()
    wallets: set[str] = set()
    new_buy_usd = 0.0
    new_wallets: set[str] = set()
    for t in w:
        usd = t.usd or 0.0
        if t.side == "buy":
            st.buy_usd += usd
            if t.wallet:
                buyers.add(t.wallet)
                if t.wallet not in prior_wallets:
                    new_buy_usd += usd
                    new_wallets.add(t.wallet)
        else:
            st.sell_usd += usd
            if t.wallet:
                sellers.add(t.wallet)
        if t.wallet:
            wallets.add(t.wallet)
    tot = st.buy_usd + st.sell_usd
    st.buyers, st.sellers = len(buyers), len(sellers)
    st.new_wallets = len(new_wallets)
    if len(w) >= min_n and tot > 0:
        st.ofi = (st.buy_usd - st.sell_usd) / tot
        st.buy_share = st.buy_usd / tot
        st.new_wallet_share_usd = new_buy_usd / st.buy_usd if st.buy_usd > 0 else 0.0
    if st.sellers > 0:
        st.buyer_seller_ratio = st.buyers / st.sellers
    elif st.buyers > 0:
        st.buyer_seller_ratio = float("inf")
    if wallets:
        st.trades_per_wallet = len(w) / len(wallets)
    if len(w) >= 2:
        st.usd_rate = tot / max(1, st.span_s)
    return st


def last_n_window(trades: list[Trade], n: int, max_age_s: int, as_of: int) -> tuple[int, int]:
    """Index range [start, end) of the last n trades no older than max_age_s."""
    end = len(trades)
    start = max(0, end - n)
    cutoff = as_of - max_age_s
    while start < end and trades[start].ts < cutoff:
        start += 1
    return start, end


def find_anchor(trades: list[Trade], s: dict[str, Any], as_of: int) -> tuple[int | None, float | None]:
    """Earliest ignition onset within the lookback: index i (window = trades[i-w+1..i]) whose USD rate
    is >= mult x the trailing rate (trailing window EXCLUDES the ignition window) with >= m buyers."""
    w = int(s["ignition_window_trades"])
    mult = float(s["anchor_rate_mult"])
    min_buyers = int(s["anchor_min_buyers"])
    trailing_s = int(s["anchor_trailing_s"])
    min_trailing = int(s["anchor_min_trailing_s"])
    lookback = int(s["anchor_lookback_s"])
    n = len(trades)
    if n < w + 1:
        return None, None
    # prefix sums of usd for O(1) range sums
    pref = [0.0]
    for t in trades:
        pref.append(pref[-1] + (t.usd or 0.0))
    first_ts = trades[0].ts
    j0 = 0  # trailing window start pointer
    for i in range(w - 1, n):
        t_end = trades[i].ts
        if t_end < as_of - lookback:
            continue
        ws = i - w + 1
        t_ws = trades[ws].ts
        # trailing window: trades with ts in [t_ws - trailing_s, t_ws) -> indices [j0, ws)
        while j0 < ws and trades[j0].ts < t_ws - trailing_s:
            j0 += 1
        trailing_dur = t_ws - max(first_ts, t_ws - trailing_s)
        if trailing_dur < min_trailing or ws - j0 == 0:
            continue
        trailing_rate = (pref[ws] - pref[j0]) / trailing_dur
        win_usd = pref[i + 1] - pref[ws]
        win_dur = max(1, t_end - t_ws)
        win_rate = win_usd / win_dur
        if trailing_rate <= 0:
            ratio = float("inf") if win_usd > 0 else 0.0
        else:
            ratio = win_rate / trailing_rate
        if ratio < mult:
            continue
        buyers = {t.wallet for t in trades[ws:i + 1] if t.side == "buy" and t.wallet}
        if len(buyers) < min_buyers:
            continue
        return i, ratio
    return None, None


def make_bars(trades: list[Trade], start_idx: int, bar_trades: int, max_bars: int) -> list[Bar]:
    bars: list[Bar] = []
    i = start_idx
    n = len(trades)
    while i < n:
        chunk = [t for t in trades[i:i + bar_trades] if t.price is not None]
        if len(chunk) < max(2, bar_trades // 2):     # partial last bar: keep only if it has substance
            break
        prices = [t.price for t in chunk]
        bars.append(Bar(i_start=i, i_end=min(n, i + bar_trades) - 1, ts_start=chunk[0].ts, ts_end=chunk[-1].ts,
                        o=prices[0], h=max(prices), l=min(prices), c=prices[-1],
                        usd=sum(t.usd or 0.0 for t in chunk), n=len(chunk)))
        i += bar_trades
    return bars[-max_bars:]


def compute(trades: list[Trade], cfg: dict[str, Any] | None = None, as_of: int | None = None) -> TapeFeatures:
    s = _settings(cfg)
    trades = sorted((t for t in trades if t.ts is not None), key=lambda t: (t.ts, t.sig))
    if as_of is not None:
        trades = [t for t in trades if t.ts <= as_of]
    if not trades:
        return TapeFeatures(n=0, as_of=as_of, oldest_ts=None, newest_ts=None)
    as_of = trades[-1].ts if as_of is None else as_of
    f = TapeFeatures(n=len(trades), as_of=as_of, oldest_ts=trades[0].ts, newest_ts=trades[-1].ts)
    priced = [t for t in trades if t.price is not None]
    f.price = priced[-1].price if priced else None
    min_n = int(s["min_window_trades"])

    def wallets_before(idx: int) -> set[str]:
        return {t.wallet for t in trades[:idx] if t.wallet}

    for name, n_tr, max_age in (("ignition", s["ignition_window_trades"], s["ignition_max_age_s"]),
                                ("ofi30", s["ofi_window_trades"], s["window_max_age_s"]),
                                ("recent", s["recent_window_trades"], s["window_max_age_s"]),
                                ("wash", s["wash_window_trades"], s["window_max_age_s"])):
        a, b = last_n_window(trades, int(n_tr), int(max_age), as_of)
        setattr(f, name, window_stats(trades, a, b, wallets_before(a), min_n))
    f.ofi_recent_nondeteriorating = None if f.recent.ofi is None else f.recent.ofi >= 0.0

    ai, ratio = find_anchor(trades, s, as_of)
    if ai is not None:
        f.anchor_idx = ai
        f.anchor_rate_ratio = ratio
        anchor_trade = trades[ai]
        f.anchor_ts = anchor_trade.ts
        # anchor window start (the onset window's first trade) defines the structural anchor price/low
        ws = ai - int(s["ignition_window_trades"]) + 1
        win = [t for t in trades[ws:ai + 1] if t.price is not None]
        f.anchor_price = win[0].price if win else anchor_trade.price
        f.anchor_low = min((t.price for t in win), default=None)
        since = trades[ws:]
        f.trades_since_anchor = len(trades) - 1 - ai
        f.seconds_since_anchor = as_of - anchor_trade.ts
        usd_sum = sum(t.usd or 0.0 for t in since)
        amt_sum = sum(t.amount or 0.0 for t in since if t.amount)
        if amt_sum > 0:
            f.avwap = usd_sum / amt_sum
        elif usd_sum > 0:
            f.avwap = sum((t.price or 0.0) * (t.usd or 0.0) for t in since) / usd_sum
        pr = [t.price for t in since if t.price is not None]
        f.max_price_since_anchor = max(pr) if pr else None
        if f.price is not None:
            if f.avwap:
                f.price_vs_avwap_pct = (f.price / f.avwap - 1) * 100
            if f.anchor_price:
                f.price_vs_anchor_pct = (f.price / f.anchor_price - 1) * 100
        bars = make_bars(trades, ws, int(s["bar_trades"]), int(s["max_bars"]))
    else:
        bars = make_bars(trades, max(0, len(trades) - int(s["bar_trades"]) * int(s["max_bars"])),
                         int(s["bar_trades"]), int(s["max_bars"]))
    f.bars_n = len(bars)
    if bars:
        f.clv_last = bars[-1].clv
        last3 = bars[-3:]
        if len(last3) >= 2:
            clvs = [b.clv for b in last3 if b.clv is not None]
            f.clv_ge_half_2of3 = sum(1 for c in clvs if c >= 0.5) >= min(2, len(last3))
            hl = sum(1 for k in range(1, len(last3)) if last3[k].l >= last3[k - 1].l)
            f.higher_lows_2of3 = hl >= min(2, len(last3) - 1)
        big = max(bars, key=lambda b: b.usd)
        f.rejection_wick_frac = big.upper_wick_frac
    return f
