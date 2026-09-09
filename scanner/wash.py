"""Wash / bot score and the anti-gates (vetoes) - Stage 2, T7.

All pure functions. Inputs: a window of trades (tape), the T6 features, and
optional enrichments (per-wallet supply share from top-traders, Birdeye tag
flows). Outputs: a WashReport with component scores, a composite wash_score,
and a list of Veto objects. Vetoes always win over the score (SPEC s10).

Components (each normalised 0..1 by config thresholds, then weighted):
  roundtrip   USD from wallets that both bought AND sold inside the window
  top3        top-3 wallets' share of window USD
  uniform     median/mean trade size (bots spray uniform sizes; humans are
              lognormal -> ratio well below 1)
  count_trap  buy:sell COUNT ratio ~1 while turnover >> liquidity
  churn       trades per wallet (same wallets cycling) and low new-wallet share

Vetoes:
  DISTRIBUTION  top-3 sellers >= X% of sell USD and one of them holds >= Y%
                of supply (holdings known) -> hard; holdings unknown -> soft
                'DISTRIBUTION_SUSPECT' when the seller share is extreme.
  REJECTION     upper wick of the highest-USD bar since anchor > threshold.
  DEV/INSIDER   tagged dev or insider wallets net selling >= share of sells.
  BUNDLER       bundler cohort >= share of sell USD.
  WASH          composite wash_score >= threshold.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from .features import TapeFeatures
from .tape import Trade

DEFAULTS: dict[str, Any] = {
    "window_trades": 60,
    "min_trades": 12,
    "weights": {"roundtrip": 0.35, "top3": 0.25, "uniform": 0.15, "count_trap": 0.15, "churn": 0.10},
    # normalisation: value at which a component saturates to 1.0 (linear from `lo`)
    "roundtrip_lo": 0.10, "roundtrip_hi": 0.50,
    "top3_lo": 0.30, "top3_hi": 0.80,
    "uniform_lo": 0.55, "uniform_hi": 0.90,
    "churn_tpw_lo": 1.5, "churn_tpw_hi": 4.0,
    "count_trap_ratio_band": [0.9, 1.1], "count_trap_turnover_min": 1.5,
    "wash_veto_score": 0.60,
    # distribution veto
    "seller_top3_share_min": 0.60, "seller_supply_pct_min": 3.0, "seller_share_suspect_min": 0.85,
    # rejection veto
    "rejection_wick_max": 0.60,
    # tag-flow vetoes (Solana, Birdeye wallet-tags-tracker)
    "dev_insider_sell_share_max": 0.20, "bundler_sell_share_max": 0.40,
}


def _settings(cfg: dict[str, Any] | None) -> dict[str, Any]:
    s = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    if cfg:
        for k, v in cfg.items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict) and isinstance(s.get(k), dict):
                s[k].update(v)
            else:
                s[k] = v
    return s


def _norm(x: float | None, lo: float, hi: float) -> float:
    if x is None:
        return 0.0
    if x <= lo:
        return 0.0
    if x >= hi:
        return 1.0
    return (x - lo) / (hi - lo)


@dataclass
class Veto:
    name: str
    hard: bool
    value: Any = None
    limit: Any = None
    detail: str = ""


@dataclass
class WashReport:
    n: int = 0
    total_usd: float = 0.0
    roundtrip_share: float | None = None
    top3_share: float | None = None
    size_uniformity: float | None = None      # median / mean of trade USD
    count_ratio: float | None = None          # buys / sells (count)
    turnover: float | None = None             # window USD / liquidity
    count_trap: bool = False
    trades_per_wallet: float | None = None
    new_wallet_share: float | None = None
    components: dict[str, float] = field(default_factory=dict)
    wash_score: float | None = None
    top3_seller_share: float | None = None
    top_seller_supply_pct: float | None = None
    vetoes: list[Veto] = field(default_factory=list)

    @property
    def hard_vetoes(self) -> list[str]:
        return [v.name for v in self.vetoes if v.hard]

    @property
    def soft_flags(self) -> list[str]:
        return [v.name for v in self.vetoes if not v.hard]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["hard_vetoes"] = self.hard_vetoes
        d["soft_flags"] = self.soft_flags
        return d


def wash_components(window: list[Trade], liquidity: float | None, new_wallet_share: float | None,
                    s: dict[str, Any]) -> WashReport:
    r = WashReport(n=len(window), new_wallet_share=new_wallet_share)
    if not window:
        return r
    usd_by_wallet: dict[str, float] = defaultdict(float)
    sides_by_wallet: dict[str, set[str]] = defaultdict(set)
    sizes: list[float] = []
    buys = sells = 0
    for t in window:
        u = t.usd or 0.0
        sizes.append(u)
        r.total_usd += u
        if t.wallet:
            usd_by_wallet[t.wallet] += u
            sides_by_wallet[t.wallet].add(t.side)
        if t.side == "buy":
            buys += 1
        else:
            sells += 1
    if r.total_usd > 0:
        rt = sum(usd_by_wallet[w] for w, sd in sides_by_wallet.items() if {"buy", "sell"} <= sd)
        r.roundtrip_share = rt / r.total_usd
        top = sorted(usd_by_wallet.values(), reverse=True)[:3]
        r.top3_share = sum(top) / r.total_usd
    if sizes and statistics.fmean(sizes) > 0:
        r.size_uniformity = statistics.median(sizes) / statistics.fmean(sizes)
    if sells > 0:
        r.count_ratio = buys / sells
    if liquidity and liquidity > 0:
        r.turnover = r.total_usd / liquidity
    lo, hi = s["count_trap_ratio_band"]
    r.count_trap = (r.count_ratio is not None and lo <= r.count_ratio <= hi
                    and r.turnover is not None and r.turnover >= float(s["count_trap_turnover_min"]))
    if usd_by_wallet:
        r.trades_per_wallet = len(window) / len(usd_by_wallet)
    churn_tpw = _norm(r.trades_per_wallet, float(s["churn_tpw_lo"]), float(s["churn_tpw_hi"]))
    churn_new = (1.0 - new_wallet_share) if new_wallet_share is not None else 0.0
    r.components = {
        "roundtrip": _norm(r.roundtrip_share, float(s["roundtrip_lo"]), float(s["roundtrip_hi"])),
        "top3": _norm(r.top3_share, float(s["top3_lo"]), float(s["top3_hi"])),
        "uniform": _norm(r.size_uniformity, float(s["uniform_lo"]), float(s["uniform_hi"])),
        "count_trap": 1.0 if r.count_trap else 0.0,
        "churn": max(churn_tpw, churn_new * 0.5),
    }
    if len(window) >= int(s["min_trades"]):
        w = s["weights"]
        r.wash_score = sum(float(w[k]) * v for k, v in r.components.items()) / max(1e-9, sum(float(x) for x in w.values()))
    return r


def distribution_check(window: list[Trade], holdings_pct: dict[str, float] | None, s: dict[str, Any]) -> tuple[Veto | None, float | None, float | None]:
    """Top-3 seller wallets' share of SELL USD; hard veto if one holds >= Y% supply, soft if holdings unknown."""
    sell_by_wallet: dict[str, float] = defaultdict(float)
    for t in window:
        if t.side == "sell" and t.wallet:
            sell_by_wallet[t.wallet] += t.usd or 0.0
    total_sell = sum(sell_by_wallet.values())
    if total_sell <= 0:
        return None, None, None
    top = sorted(sell_by_wallet.items(), key=lambda kv: -kv[1])[:3]
    share = sum(v for _, v in top) / total_sell
    top_pct = None
    if holdings_pct:
        pcts = [holdings_pct.get(w) for w, _ in top if holdings_pct.get(w) is not None]
        top_pct = max(pcts) if pcts else None
    if share < float(s["seller_top3_share_min"]):
        return None, share, top_pct
    if top_pct is not None and top_pct >= float(s["seller_supply_pct_min"]):
        return Veto("DISTRIBUTION", True, round(share, 3), s["seller_top3_share_min"],
                    f"top-3 sellers {share:.0%} of sell USD; largest holds {top_pct:.1f}% supply"), share, top_pct
    if holdings_pct is None and share >= float(s["seller_share_suspect_min"]):
        return Veto("DISTRIBUTION_SUSPECT", False, round(share, 3), s["seller_share_suspect_min"],
                    "holdings unknown; extreme seller concentration"), share, top_pct
    return None, share, top_pct


def rejection_check(f: TapeFeatures, s: dict[str, Any]) -> Veto | None:
    if f.rejection_wick_frac is not None and f.rejection_wick_frac > float(s["rejection_wick_max"]):
        return Veto("REJECTION", True, round(f.rejection_wick_frac, 3), s["rejection_wick_max"],
                    "long upper wick on the highest-volume bar since anchor")
    return None


def parse_tag_flows(payload: dict[str, Any] | None) -> dict[str, dict[str, float]]:
    """Birdeye wallet-tags-tracker -> {tag: {buy_usd, sell_usd, buyers, sellers}} summed over buckets.
    Defensive: groups may be {tag: [bucket,...]} or {tag: {...}}; bucket keys as documented."""
    out: dict[str, dict[str, float]] = {}
    groups = (payload or {}).get("groups") or {}
    if not isinstance(groups, dict):
        return out
    # documented shape: groups.tags.{dev,sniper,smart_trader,kol}: [buckets]; groups.top_10_holder: [buckets]
    series: dict[str, Any] = {}
    if isinstance(groups.get("tags"), dict):
        series.update(groups["tags"])
    if "top_10_holder" in groups:
        series["top_10_holder"] = groups["top_10_holder"]
    if not series:   # tolerate a flat {tag: buckets} shape too
        series = {k: v for k, v in groups.items() if k != "tag_combinations"}
    for tag, val in series.items():
        buckets = val if isinstance(val, list) else [val] if isinstance(val, dict) else []
        agg = {"buy_usd": 0.0, "sell_usd": 0.0, "buyers": 0.0, "sellers": 0.0}
        for b in buckets:
            if not isinstance(b, dict):
                continue
            # a bucket may itself hold nested series (e.g. {"items":[...]})
            inner = b.get("items") if isinstance(b.get("items"), list) else [b]
            for x in inner:
                if not isinstance(x, dict):
                    continue
                agg["buy_usd"] += float(x.get("volume_buy_usd") or 0.0)
                agg["sell_usd"] += float(x.get("volume_sell_usd") or 0.0)
                agg["buyers"] += float(x.get("wallet_buy_count") or 0.0)
                agg["sellers"] += float(x.get("wallet_sell_count") or 0.0)
        out[str(tag)] = agg
    return out


def tag_vetoes(flows: dict[str, dict[str, float]], window_sell_usd: float, s: dict[str, Any]) -> list[Veto]:
    """Vetoes from tagged-cohort selling relative to the window's sell USD."""
    out: list[Veto] = []
    if not flows or window_sell_usd <= 0:
        return out
    dev_ins = sum(flows.get(t, {}).get("sell_usd", 0.0) for t in ("dev", "insider"))
    if dev_ins / window_sell_usd >= float(s["dev_insider_sell_share_max"]):
        out.append(Veto("DEV_INSIDER_SELLING", True, round(dev_ins / window_sell_usd, 3),
                        s["dev_insider_sell_share_max"], "dev/insider wallets are net sellers"))
    bund = flows.get("bundler", {}).get("sell_usd", 0.0)
    if bund / window_sell_usd >= float(s["bundler_sell_share_max"]):
        out.append(Veto("BUNDLER_SELLING", True, round(bund / window_sell_usd, 3),
                        s["bundler_sell_share_max"], "bundler cohort unloading"))
    return out


def evaluate(trades: list[Trade], f: TapeFeatures, cfg: dict[str, Any] | None = None,
             liquidity: float | None = None, holdings_pct: dict[str, float] | None = None,
             tag_flows: dict[str, dict[str, float]] | None = None, as_of: int | None = None) -> WashReport:
    s = _settings(cfg)
    ts = sorted((t for t in trades if t.ts is not None), key=lambda t: (t.ts, t.sig))
    if as_of is not None:
        ts = [t for t in ts if t.ts <= as_of]
    window = ts[-int(s["window_trades"]):]
    r = wash_components(window, liquidity, f.wash.new_wallet_share_usd, s)
    if r.wash_score is not None and r.wash_score >= float(s["wash_veto_score"]):
        r.vetoes.append(Veto("WASH", True, round(r.wash_score, 3), s["wash_veto_score"],
                             "composite wash/bot score"))
    v, share, top_pct = distribution_check(window, holdings_pct, s)
    r.top3_seller_share, r.top_seller_supply_pct = share, top_pct
    if v:
        r.vetoes.append(v)
    rj = rejection_check(f, s)
    if rj:
        r.vetoes.append(rj)
    sell_usd = sum((t.usd or 0.0) for t in window if t.side == "sell")
    r.vetoes.extend(tag_vetoes(tag_flows or {}, sell_usd, s))
    return r


def holdings_from_top_traders(items: list[dict[str, Any]], supply: float | None) -> dict[str, float]:
    """Per-wallet supply % from token_top_traders rows (holdVolume in token units)."""
    if not supply or supply <= 0:
        return {}
    out: dict[str, float] = {}
    for it in items:
        w = it.get("owner")
        hv = it.get("holdVolume")
        if w and hv is not None:
            try:
                out[str(w)] = 100.0 * float(hv) / supply
            except (TypeError, ValueError):
                continue
    return out
