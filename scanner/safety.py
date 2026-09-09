"""Stage 3 safety (T10: Solana; T11 adds the Robinhood honeypot simulator).

Solana sources (both plan-independent in practice):
  - free Solana RPC getAccountInfo(mint, jsonParsed): mint authority, freeze
    authority, token program (spl-token vs token-2022).
  - Birdeye holder-profile (25 CU, Standard+): top-10 % of supply and the
    cohorts bundler / sniper / insider / dev / smart_trader / kol with
    holder_count, percent_of_supply, buy/sell USD and pnl.

Verdict: UNSAFE if any HARD check fails; SAFE if every hard check passed;
UNKNOWN if a hard check could not be evaluated (no data) - never SAFE by
default. Soft checks produce flags. `bonus` (0..5) feeds the score.
Results are cached in `safety` for cache_s; T12 re-checks after alerts.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

import aiohttp

from .birdeye import BirdeyeClient, BirdeyeError, EndpointUnavailable
from .config import ChainConfig
from .ledger import CULedger
from .plans import cu_cost

log = logging.getLogger("safety")

DEFAULTS: dict[str, Any] = {
    "cache_s": 600,
    "daily_cu_budget": 20_000,
    "rpc_timeout_s": 12,
    "solana": {
        "top10_max_pct": 35.0, "top10_bonus_pct": 20.0,
        "dev_max_pct": 10.0, "dev_bonus_pct": 3.0,
        "insider_max_pct": 10.0,
        "bundler_flag_pct": 15.0, "bundler_bonus_pct": 5.0,
        "sniper_flag_pct": 20.0,
        "smart_bonus_pct": 1.0,
        "min_holders": 30,
    },
}


def _settings(cfg: dict[str, Any] | None) -> dict[str, Any]:
    s = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for k, v in (cfg or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(s.get(k), dict):
            s[k].update(v)
        else:
            s[k] = v
    return s


@dataclass
class Check:
    name: str
    passed: bool | None          # None = could not evaluate
    value: Any = None
    limit: Any = None
    hard: bool = True


@dataclass
class SafetyResult:
    chain: str
    address: str
    checked_ts: int
    verdict: str                 # SAFE | UNSAFE | UNKNOWN
    checks: list[Check] = field(default_factory=list)
    bonus: float = 0.0
    flags: list[str] = field(default_factory=list)      # soft
    reasons: list[str] = field(default_factory=list)    # hard fails
    sources: dict[str, Any] = field(default_factory=dict)
    from_cache: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---- pure evaluators ---------------------------------------------------------------------

def parse_mint_account(rpc_json: dict[str, Any] | None) -> dict[str, Any] | None:
    """getAccountInfo(jsonParsed) -> {mint_authority, freeze_authority, program, decimals, supply} or None."""
    try:
        value = (rpc_json or {}).get("result", {}).get("value")
        if not value:
            return None
        data = value.get("data") or {}
        info = (data.get("parsed") or {}).get("info") or {}
        if not info:
            return None
        return {"mint_authority": info.get("mintAuthority"), "freeze_authority": info.get("freezeAuthority"),
                "program": data.get("program"), "decimals": info.get("decimals"), "supply": info.get("supply"),
                "extensions": [e.get("extension") for e in (info.get("extensions") or []) if isinstance(e, dict)]}
    except AttributeError:
        return None


def parse_holder_profile(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Birdeye holder-profile -> {top10_pct, holders, cohorts: {tag: {holders, pct, pnl, buy_usd, sell_usd}}}."""
    if not payload:
        return None
    tok = payload.get("token") or {}
    top10 = (tok.get("top10_holder") or {}).get("percent_of_supply")
    summ = payload.get("holder_summary") or {}
    cohorts: dict[str, dict[str, float | None]] = {}
    for t in payload.get("tags") or []:
        if not isinstance(t, dict) or not t.get("tag"):
            continue
        def f(v: Any) -> float | None:
            try:
                return None if v is None else float(v)
            except (TypeError, ValueError):
                return None
        cohorts[str(t["tag"])] = {"holders": f(t.get("holder_count")), "pct": f(t.get("percent_of_supply")),
                                  "pnl": f(t.get("pnl")), "buy_usd": f(t.get("buy_volume_usd")),
                                  "sell_usd": f(t.get("sell_volume_usd"))}
    return {"top10_pct": float(top10) if top10 is not None else None,
            "holders": int(summ["total_holder"]) if summ.get("total_holder") is not None else None,
            "liquidity": tok.get("liquidity"), "market_cap": tok.get("market_cap"),
            "creation_time": tok.get("creation_time"), "cohorts": cohorts}


def evaluate_solana(mint: dict[str, Any] | None, profile: dict[str, Any] | None, s: dict[str, Any]) -> tuple[list[Check], float]:
    p = s["solana"]
    checks: list[Check] = []
    bonus = 0.0
    # authorities (hard)
    if mint is None:
        checks += [Check("mint_authority", None), Check("freeze_authority", None)]
    else:
        checks.append(Check("mint_authority", mint.get("mint_authority") is None, mint.get("mint_authority"), None))
        checks.append(Check("freeze_authority", mint.get("freeze_authority") is None, mint.get("freeze_authority"), None))
        if mint.get("program") == "spl-token-2022":
            ext = mint.get("extensions") or []
            risky = [e for e in ext if e in ("transferHook", "transferFeeConfig", "permanentDelegate", "defaultAccountState")]
            checks.append(Check("token2022_extensions", not risky, risky, [], hard=False))
    # holder structure
    if profile is None:
        checks += [Check("top10", None), Check("dev_holdings", None), Check("insider_holdings", None)]
    else:
        top10 = profile.get("top10_pct")
        checks.append(Check("top10", None if top10 is None else top10 <= p["top10_max_pct"], top10, p["top10_max_pct"]))
        if top10 is not None and top10 <= p["top10_bonus_pct"]:
            bonus += 2.0
        holders = profile.get("holders")
        checks.append(Check("min_holders", None if holders is None else holders >= p["min_holders"], holders,
                            p["min_holders"], hard=False))
        co = profile.get("cohorts") or {}
        def pct(tag: str) -> float | None:
            return (co.get(tag) or {}).get("pct")
        dev = pct("dev")
        checks.append(Check("dev_holdings", True if dev is None else dev <= p["dev_max_pct"], dev, p["dev_max_pct"]))
        if dev is not None and dev <= p["dev_bonus_pct"]:
            bonus += 1.0
        ins = pct("insider")
        checks.append(Check("insider_holdings", True if ins is None else ins <= p["insider_max_pct"], ins, p["insider_max_pct"]))
        bund = pct("bundler")
        checks.append(Check("bundler_holdings", True if bund is None else bund < p["bundler_flag_pct"], bund,
                            p["bundler_flag_pct"], hard=False))
        if bund is not None and bund <= p["bundler_bonus_pct"]:
            bonus += 1.0
        snip = pct("sniper")
        checks.append(Check("sniper_holdings", True if snip is None else snip < p["sniper_flag_pct"], snip,
                            p["sniper_flag_pct"], hard=False))
        smart = pct("smart_trader")
        if smart is not None and smart >= p["smart_bonus_pct"]:
            bonus += 1.0
        checks.append(Check("smart_trader_present", (smart or 0.0) >= p["smart_bonus_pct"], smart, p["smart_bonus_pct"], hard=False))
    return checks, min(5.0, bonus)


def verdict_of(checks: list[Check]) -> tuple[str, list[str], list[str]]:
    hard_fail = [c.name for c in checks if c.hard and c.passed is False]
    unknown = [c.name for c in checks if c.hard and c.passed is None]
    flags = [c.name for c in checks if not c.hard and c.passed is False]
    if hard_fail:
        return "UNSAFE", hard_fail, flags
    if unknown:
        return "UNKNOWN", unknown, flags
    return "SAFE", [], flags


# ---- checker with sources + cache ----------------------------------------------------------

class SafetyChecker:
    def __init__(self, conn: sqlite3.Connection, client: BirdeyeClient | None, ledger: CULedger | None,
                 settings: dict[str, Any], daily_cu_cap: int, rpc_urls: dict[str, str | None],
                 clock: Callable[[], float] = time.time, session: aiohttp.ClientSession | None = None) -> None:
        self.conn = conn
        self.client = client
        self.ledger = ledger
        self.s = _settings(settings)
        self.daily_cu_cap = daily_cu_cap
        self.rpc_urls = rpc_urls
        self._clock = clock
        self._session = session
        self.cu_today = 0
        self._day = int(clock() // 86400)
        self.calls = 0
        self.cache_hits = 0
        self.rpc_errors = 0

    async def _rpc(self, url: str, method: str, params: list[Any]) -> dict[str, Any] | None:
        """JSON-RPC POST with a browser-like User-Agent (the Robinhood RPC returns 403 to Python's default UA)
        and a small pacing delay + one retry on 429 (the public RPC rate-limits bursts)."""
        import asyncio as _asyncio
        if self._session is None:
            self._session = aiohttp.ClientSession(headers={"user-agent": str(self.s.get("rpc_user_agent") or "momentum-scanner/1.0")})
        rps = float((self.s.get("evm") or {}).get("rpc_rps", 3))
        for attempt in range(2):
            try:
                await _asyncio.sleep(1.0 / rps if rps > 0 else 0)
                async with self._session.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                              timeout=aiohttp.ClientTimeout(total=float(self.s["rpc_timeout_s"]))) as r:
                    if r.status == 429 and attempt == 0:
                        await _asyncio.sleep(2.0)
                        continue
                    if r.status != 200:
                        self.rpc_errors += 1
                        log.warning("rpc %s HTTP %s", method, r.status)
                        return None
                    return await r.json()
            except (aiohttp.ClientError, TimeoutError, ValueError) as e:  # noqa: PERF203
                self.rpc_errors += 1
                log.warning("rpc %s failed: %s", method, e)
                return None
        return None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _budget_ok(self, cu: int) -> bool:
        d = int(self._clock() // 86400)
        if d != self._day:
            self._day, self.cu_today = d, 0
        if self.cu_today + cu > int(self.s["daily_cu_budget"]):
            return False
        if self.ledger is not None and self.ledger.total_since(d * 86400) + cu > self.daily_cu_cap:
            return False
        return True

    def cached(self, chain: str, address: str, max_age_s: int | None = None) -> SafetyResult | None:
        r = self.conn.execute("SELECT checked_ts, verdict, result_json FROM safety WHERE chain=? AND address=?",
                              (chain, address)).fetchone()
        if r is None:
            return None
        max_age = int(self.s["cache_s"]) if max_age_s is None else max_age_s
        if self._clock() - int(r["checked_ts"]) > max_age:
            return None
        d = json.loads(r["result_json"])
        res = SafetyResult(chain=chain, address=address, checked_ts=int(r["checked_ts"]), verdict=r["verdict"],
                           checks=[Check(**c) for c in d.get("checks", [])], bonus=float(d.get("bonus", 0.0)),
                           flags=list(d.get("flags", [])), reasons=list(d.get("reasons", [])),
                           sources=dict(d.get("sources", {})), from_cache=True)
        self.cache_hits += 1
        return res

    def _store(self, res: SafetyResult) -> None:
        self.conn.execute(
            "INSERT INTO safety(chain, address, checked_ts, verdict, result_json) VALUES(?,?,?,?,?) "
            "ON CONFLICT(chain, address) DO UPDATE SET checked_ts=excluded.checked_ts, verdict=excluded.verdict, "
            "result_json=excluded.result_json",
            (res.chain, res.address, res.checked_ts, res.verdict,
             json.dumps({"checks": [asdict(c) for c in res.checks], "bonus": res.bonus, "flags": res.flags,
                         "reasons": res.reasons, "sources": res.sources}, separators=(",", ":"), default=str)))

    async def check(self, ch: ChainConfig, address: str, force: bool = False) -> SafetyResult:
        if not force:
            c = self.cached(ch.name, address)
            if c is not None:
                return c
        now = int(self._clock())
        if ch.birdeye_chain != "solana":
            res = await self._check_evm(ch, address, now)
            self._store(res)
            return res
        mint = None
        url = self.rpc_urls.get("solana")
        if url:
            mint = parse_mint_account(await self._rpc(url, "getAccountInfo", [address, {"encoding": "jsonParsed"}]))
        profile = None
        cu, _ = cu_cost("token_holder_profile")
        if self.client is not None and self._budget_ok(cu):
            try:
                profile = parse_holder_profile(await self.client.token_holder_profile(ch.birdeye_chain, address))
                self.calls += 1
                self.cu_today += cu
            except (EndpointUnavailable, BirdeyeError) as e:
                log.warning("holder profile failed for %s: %s", address[:8], e)
        checks, bonus = evaluate_solana(mint, profile, self.s)
        verdict, reasons, flags = verdict_of(checks)
        res = SafetyResult(ch.name, address, now, verdict, checks, bonus if verdict == "SAFE" else 0.0, flags, reasons,
                           {"mint": mint, "profile": profile})
        self._store(res)
        return res

    # ---- EVM (T11) -----------------------------------------------------------------------------
    def _recent_buyers(self, chain: str, address: str, limit: int = 5) -> list[str]:
        rows = self.conn.execute(
            "SELECT wallet, SUM(usd) AS u FROM trades WHERE chain=? AND address=? AND side='buy' AND wallet IS NOT NULL "
            "GROUP BY wallet ORDER BY u DESC LIMIT ?", (chain, address, limit)).fetchall()
        return [r["wallet"] for r in rows]

    async def _check_evm(self, ch: ChainConfig, address: str, now: int) -> SafetyResult:
        from . import evm_safety as ev
        s = ev._settings(self.s.get("evm"))
        url = self.rpc_urls.get(ch.name)
        if not url:
            return SafetyResult(ch.name, address, now, "UNKNOWN", [Check("honeypot_sim", None, "no rpc url")], 0.0, [],
                                ["honeypot_sim"], {"note": "no RPC url configured"})
        rpc = lambda m, p: self._rpc(url, m, p)  # noqa: E731
        counterparty = (s.get("pool_manager") or {}).get(ch.name)
        sources: dict[str, Any] = {"counterparty": counterparty}
        supply = await ev.read_uint_call(rpc, address, ev.SEL_TOTAL_SUPPLY)
        decimals = await ev.read_uint_call(rpc, address, "0x313ce567")
        owner_state, owner_addr = await ev.read_owner(rpc, address)
        sources.update({"supply": supply, "decimals": decimals, "owner": owner_addr, "owner_state": owner_state})
        sim = ev.SimReport(holder=None, counterparty=counterparty or "", error="no pool counterparty configured")
        if counterparty:
            cp_bal = await ev.read_uint_call(rpc, address, ev.calldata_balance_of(counterparty))
            holder, holder_bal = None, None
            for w in self._recent_buyers(ch.name, address):
                b = await ev.read_uint_call(rpc, address, ev.calldata_balance_of(w))
                if b and b > 0:
                    holder, holder_bal = w, b
                    break
            sim = await ev.simulate_paths(rpc, address, holder, holder_bal, counterparty, cp_bal, s)
            sources["sim"] = {"holder": sim.holder, "error": sim.error,
                              "paths": [{"name": p.name, "ok": p.ok, "sent": p.sent, "received": p.received,
                                         "tax_pct": p.tax_pct, "error": p.error} for p in sim.paths]}
        top10 = None
        cu, _ = cu_cost("token_top_traders")
        if self.client is not None and self._budget_ok(cu):
            try:
                traders = await self.client.token_top_traders(ch.birdeye_chain, address, time_frame="24h",
                                                              sort_by="volume", limit=10)
                self.calls += 1
                self.cu_today += cu
                top10 = ev.top10_proxy_pct(traders, supply, decimals)
                sources["top10_proxy_pct"] = top10
            except (EndpointUnavailable, BirdeyeError) as e:
                log.warning("top traders failed for %s: %s", address[:8], e)
        raw_checks, bonus = ev.evaluate_evm(sim, owner_state, top10, s)
        checks = [Check(c.name, c.passed, c.value, c.limit, c.hard) for c in raw_checks]
        verdict, reasons, flags = verdict_of(checks)
        return SafetyResult(ch.name, address, now, verdict, checks, bonus if verdict == "SAFE" else 0.0, flags, reasons, sources)
