"""EVM (Robinhood Chain) safety - T11. Rebuilt from the old RH gem finder's design.

No third-party security API covers chain 4663 and Blockscout is now behind a
Cloudflare challenge, so everything here is chain-native via the public RPC:

  HONEYPOT / TAX (eth_simulateV1, state-persisting call batches - verified
  supported on rpc.mainnet.chain.robinhood.com 2026-09-09):
    sell path     a real holder (top recent buyer from our tape) transfers to
                  the pool counterparty (Uniswap v4 PoolManager, which holds all
                  v4 token balances; or the v2/v3 pool address). Revert -> HONEYPOT.
    buy path      the pool counterparty transfers tokens out to a fresh wallet.
    transfer path fresh wallet -> another fresh wallet.
    taxes         measured by balanceOf deltas, never by what the contract
                  declares. Any path > max_tax_pct -> TAXED (hard).
  OWNER           owner() via eth_call: reverts -> no owner; zero -> renounced;
                  address -> owner_active (soft; the old bot's default too).
  CONCENTRATION   Blockscout holders are unreachable; the RPC cannot serve
                  full-range Transfer logs (query timeouts). Proxy from Birdeye
                  token_top_traders holdVolume vs totalSupply: a LOWER BOUND on
                  top-10 concentration. Exceeds the limit -> UNSAFE (proof);
                  otherwise soft flag top10_unverified.
  Sim errors / RPC failures -> UNKNOWN, never SAFE.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("evm_safety")

SEL_TRANSFER = "0xa9059cbb"
SEL_BALANCE_OF = "0x70a08231"
SEL_TOTAL_SUPPLY = "0x18160ddd"
SEL_OWNER = "0x8da5cb5b"
ZERO = "0x" + "0" * 40

DEFAULTS: dict[str, Any] = {
    "max_tax_pct": 5.0,
    "sim_amount_frac": 0.01,        # fraction of the holder's balance to move in the sim
    "top10_max_pct": 35.0,
    "pool_manager": {"robinhood": "0x8366a39cc670b4001a1121b8f6a443a643e40951"},
    "eth_override_wei": 10**18,
}

RpcFn = Callable[[str, list[Any]], Awaitable[dict[str, Any] | None]]


def _pad_addr(addr: str) -> str:
    return addr[2:].lower().rjust(64, "0")


def _pad_uint(v: int) -> str:
    return hex(v)[2:].rjust(64, "0")


def calldata_transfer(to: str, amount: int) -> str:
    return SEL_TRANSFER + _pad_addr(to) + _pad_uint(amount)


def calldata_balance_of(addr: str) -> str:
    return SEL_BALANCE_OF + _pad_addr(addr)


def _uint(ret: str | None) -> int | None:
    if not ret or ret == "0x":
        return None
    try:
        return int(ret, 16)
    except ValueError:
        return None


def fresh_address() -> str:
    return "0x" + secrets.token_hex(20)


@dataclass
class PathResult:
    name: str
    ok: bool | None            # None = could not evaluate
    sent: int = 0
    received: int | None = None
    tax_pct: float | None = None
    error: str | None = None


@dataclass
class SimReport:
    holder: str | None
    counterparty: str
    paths: list[PathResult] = field(default_factory=list)
    error: str | None = None

    def path(self, name: str) -> PathResult | None:
        return next((p for p in self.paths if p.name == name), None)


def parse_sim_block(block: dict[str, Any] | None, n_calls: int) -> list[tuple[bool, str | None, str | None]]:
    """-> [(status_ok, returnData, error_message)] per call, padded to n_calls with failures."""
    out: list[tuple[bool, str | None, str | None]] = []
    calls = (block or {}).get("calls") or []
    for c in calls:
        ok = str(c.get("status", "0x0")).lower() in ("0x1", "1", "true")
        err = None
        if not ok:
            e = c.get("error")
            err = (e.get("message") if isinstance(e, dict) else str(e)) if e else "reverted"
        out.append((ok, c.get("returnData"), err))
    while len(out) < n_calls:
        out.append((False, None, "missing"))
    return out


async def simulate_paths(rpc: RpcFn, token: str, holder: str | None, holder_balance: int | None,
                         counterparty: str, counterparty_balance: int | None, s: dict[str, Any]) -> SimReport:
    """Three state-persisting blocks: sell (holder -> pool), buy (pool -> fresh), transfer (fresh -> fresh2)."""
    rep = SimReport(holder=holder, counterparty=counterparty)
    frac = float(s["sim_amount_frac"])
    fresh, fresh2 = fresh_address(), fresh_address()
    eth = hex(int(s["eth_override_wei"]))
    overrides = {counterparty: {"balance": eth}, fresh: {"balance": eth}}
    if holder:
        overrides[holder] = {"balance": eth}
    blocks: list[dict[str, Any]] = []
    plan: list[tuple[str, int]] = []
    sell_amt = int((holder_balance or 0) * frac) if holder and holder_balance else 0
    if holder and sell_amt > 0:
        blocks.append({"stateOverrides": overrides, "calls": [
            {"to": token, "data": calldata_balance_of(counterparty)},
            {"from": holder, "to": token, "data": calldata_transfer(counterparty, sell_amt)},
            {"to": token, "data": calldata_balance_of(counterparty)}]})
        plan.append(("sell", sell_amt))
    buy_amt = int((counterparty_balance or 0) * frac)
    if buy_amt <= 0:
        rep.error = "pool counterparty holds no tokens"
        return rep
    blocks.append({"calls": [
        {"to": token, "data": calldata_balance_of(fresh)},
        {"from": counterparty, "to": token, "data": calldata_transfer(fresh, buy_amt)},
        {"to": token, "data": calldata_balance_of(fresh)}]})
    plan.append(("buy", buy_amt))
    # transfer path moves what the fresh wallet actually received (measured in the buy block) - we use half of buy_amt
    xfer_amt = max(1, buy_amt // 2)
    blocks.append({"calls": [
        {"to": token, "data": calldata_balance_of(fresh2)},
        {"from": fresh, "to": token, "data": calldata_transfer(fresh2, xfer_amt)},
        {"to": token, "data": calldata_balance_of(fresh2)}]})
    plan.append(("transfer", xfer_amt))
    if not holder or sell_amt <= 0:
        # fallback sell path: the fresh wallet (which now holds tokens) sells to the pool
        sell_back = max(1, buy_amt // 4)
        blocks.append({"calls": [
            {"to": token, "data": calldata_balance_of(counterparty)},
            {"from": fresh, "to": token, "data": calldata_transfer(counterparty, sell_back)},
            {"to": token, "data": calldata_balance_of(counterparty)}]})
        plan.append(("sell", sell_back))
    res = await rpc("eth_simulateV1", [{"blockStateCalls": blocks, "validation": False, "traceTransfers": False}, "latest"])
    if not res or "error" in res or not isinstance(res.get("result"), list):
        rep.error = (res or {}).get("error", {}).get("message", "no result") if isinstance((res or {}).get("error"), dict) else "no result"
        return rep
    results = res["result"]
    for (name, amt), block in zip(plan, results + [None] * (len(plan) - len(results))):
        calls = parse_sim_block(block, 3)
        pre_ok, pre, _ = calls[0]
        ok, _, err = calls[1]
        post_ok, post, _ = calls[2]
        pr = PathResult(name=name, ok=None, sent=amt)
        if not ok:
            pr.ok = False
            pr.error = err
        elif pre_ok and post_ok and _uint(pre) is not None and _uint(post) is not None:
            recv = _uint(post) - _uint(pre)
            pr.received = recv
            pr.tax_pct = max(0.0, (1 - recv / amt) * 100) if amt > 0 else None
            pr.ok = True
        else:
            pr.ok = None
            pr.error = "balance read failed"
        rep.paths.append(pr)
    return rep


async def read_owner(rpc: RpcFn, token: str) -> tuple[str, str | None]:
    """-> ('none' | 'renounced' | 'active', owner_address)."""
    r = await rpc("eth_call", [{"to": token, "data": SEL_OWNER}, "latest"])
    if not r or "error" in r or not r.get("result") or r["result"] == "0x":
        return "none", None
    raw = r["result"]
    if len(raw) < 66:
        return "none", None
    addr = "0x" + raw[-40:]
    return ("renounced" if addr == ZERO else "active"), addr


async def read_uint_call(rpc: RpcFn, token: str, data: str) -> int | None:
    r = await rpc("eth_call", [{"to": token, "data": data}, "latest"])
    if not r or "error" in r:
        return None
    return _uint(r.get("result"))


def top10_proxy_pct(top_traders: list[dict[str, Any]], supply: int | None, decimals: int | None) -> float | None:
    """Lower bound on top-10 concentration from Birdeye top-traders holdVolume (token units)."""
    if not supply or supply <= 0 or decimals is None:
        return None
    holds: list[float] = []
    for it in top_traders:
        hv = it.get("holdVolume")
        try:
            if hv is not None:
                holds.append(float(hv))
        except (TypeError, ValueError):
            continue
    if not holds:
        return None
    top = sorted(holds, reverse=True)[:10]
    return 100.0 * sum(top) / (supply / (10 ** decimals))


@dataclass
class EvmCheck:
    name: str
    passed: bool | None
    value: Any = None
    limit: Any = None
    hard: bool = True


def evaluate_evm(sim: SimReport, owner_state: str, top10_pct: float | None, s: dict[str, Any]) -> tuple[list[EvmCheck], float]:
    checks: list[EvmCheck] = []
    max_tax = float(s["max_tax_pct"])
    if sim.error and not sim.paths:
        checks += [EvmCheck("honeypot_sim", None, sim.error)]
    else:
        for name in ("sell", "buy", "transfer"):
            p = sim.path(name)
            if p is None:
                checks.append(EvmCheck(f"{name}_path", None))
                continue
            if p.ok is None:
                checks.append(EvmCheck(f"{name}_path", None, p.error))
            elif not p.ok:
                checks.append(EvmCheck(f"{name}_path", False, p.error or "reverted"))
            else:
                checks.append(EvmCheck(f"{name}_path", True))
                checks.append(EvmCheck(f"{name}_tax", p.tax_pct is not None and p.tax_pct <= max_tax,
                                       round(p.tax_pct, 3) if p.tax_pct is not None else None, max_tax))
    checks.append(EvmCheck("owner", owner_state != "active", owner_state, "renounced|none", hard=False))
    # Concentration is only a LOWER BOUND here (top traders' holdings). It can PROVE a token unsafe, but its
    # absence must not block: unknown -> soft flag, not UNKNOWN (which would cap every Robinhood token forever).
    if top10_pct is None:
        checks.append(EvmCheck("top10_unknown", False, "no holder data (Blockscout blocked, proxy empty)", None, hard=False))
    else:
        checks.append(EvmCheck("top10_proxy", top10_pct <= float(s["top10_max_pct"]), round(top10_pct, 2), s["top10_max_pct"]))
        checks.append(EvmCheck("top10_unverified", False, "lower bound from top traders only", None, hard=False))
    bonus = 0.0
    taxes = [c for c in checks if c.name.endswith("_tax")]
    if taxes and all(c.passed and (c.value or 0) == 0 for c in taxes):
        bonus += 2.0
    if owner_state in ("renounced", "none"):
        bonus += 1.0
    if top10_pct is not None and top10_pct <= 10.0:
        bonus += 1.0
    return checks, bonus


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
