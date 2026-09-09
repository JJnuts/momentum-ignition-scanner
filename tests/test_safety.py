import json
from pathlib import Path

import pytest

from scanner.birdeye import BirdeyeError
from scanner.config import ChainConfig
from scanner.db import open_db
from scanner.ledger import CULedger
from scanner.safety import (SafetyChecker, evaluate_solana, parse_holder_profile, parse_mint_account, verdict_of,
                            _settings)

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
SCFG = CFG["safety"]
S = _settings(SCFG)
NOW = 1_788_866_000
SOL = ChainConfig("solana", True, "solana", 60, {}, {})
RH = ChainConfig("robinhood", True, "robinhood", 120, {}, {})


def rpc_mint(mint_auth=None, freeze_auth=None, program="spl-token", extensions=None):
    info = {"mintAuthority": mint_auth, "freezeAuthority": freeze_auth, "decimals": 9, "supply": "1", "isInitialized": True}
    if extensions is not None:
        info["extensions"] = [{"extension": e} for e in extensions]
    return {"jsonrpc": "2.0", "result": {"value": {"data": {"program": program, "parsed": {"info": info}}}}}


def profile(top10=34.07, holders=2108, **cohorts):
    """Shape observed live 2026-09-09."""
    tags = [{"tag": tag, "holder_count": 10, "hold_amount": "1", "percent_of_supply": pct, "buy_volume_usd": "100",
             "sell_volume_usd": "50", "pnl": "5"} for tag, pct in cohorts.items()]
    return {"token": {"top10_holder": {"hold_amount": "3", "percent_of_supply": top10}, "liquidity": 1.0,
                      "market_cap": 2.0, "creation_time": 1786143868},
            "holder_summary": {"total_holder": holders, "total_holding": 1.0, "percent_of_supply": 100}, "tags": tags}


# ---- parsers ----------------------------------------------------------------------------

def test_parse_mint_account_real_shape():
    m = parse_mint_account(rpc_mint())
    assert m["mint_authority"] is None and m["freeze_authority"] is None and m["program"] == "spl-token"
    m2 = parse_mint_account(rpc_mint(mint_auth="Auth111", program="spl-token-2022", extensions=["transferHook"]))
    assert m2["mint_authority"] == "Auth111" and m2["extensions"] == ["transferHook"]
    assert parse_mint_account({"result": {"value": None}}) is None and parse_mint_account(None) is None
    assert parse_mint_account({"result": {"value": {"data": ["base64", "x"]}}}) is None


def test_parse_holder_profile_real_shape():
    p = parse_holder_profile(profile(bundler=4.92, dev=1.5, smart_trader=2.0))
    assert p["top10_pct"] == pytest.approx(34.07) and p["holders"] == 2108
    assert p["cohorts"]["bundler"]["pct"] == pytest.approx(4.92) and p["cohorts"]["dev"]["pnl"] == 5.0
    assert parse_holder_profile({}) is None and parse_holder_profile(None) is None


# ---- evaluation -----------------------------------------------------------------------------

def test_clean_token_is_safe_with_bonus():
    checks, bonus = evaluate_solana(parse_mint_account(rpc_mint()),
                                    parse_holder_profile(profile(top10=15, dev=1.0, bundler=2.0, smart_trader=1.5)), S)
    v, reasons, flags = verdict_of(checks)
    assert v == "SAFE" and reasons == [] and flags == []
    assert bonus == 5.0     # top10<=20 (+2), dev<=3 (+1), bundler<=5 (+1), smart>=1 (+1)


@pytest.mark.parametrize("mint_kw, prof_kw, reason", [
    (dict(mint_auth="X"), {}, "mint_authority"),
    (dict(freeze_auth="Y"), {}, "freeze_authority"),
    ({}, dict(top10=60), "top10"),
    ({}, dict(dev=25), "dev_holdings"),
    ({}, dict(insider=12), "insider_holdings"),
])
def test_hard_fails_are_unsafe(mint_kw, prof_kw, reason):
    checks, bonus = evaluate_solana(parse_mint_account(rpc_mint(**mint_kw)), parse_holder_profile(profile(**prof_kw)), S)
    v, reasons, _ = verdict_of(checks)
    assert v == "UNSAFE" and reason in reasons


def test_soft_flags_do_not_block():
    checks, _ = evaluate_solana(parse_mint_account(rpc_mint(program="spl-token-2022", extensions=["transferFeeConfig"])),
                                parse_holder_profile(profile(top10=30, bundler=40, sniper=25, holders=12)), S)
    v, reasons, flags = verdict_of(checks)
    assert v == "SAFE" and reasons == []
    assert set(flags) >= {"token2022_extensions", "bundler_holdings", "sniper_holdings", "min_holders"}


def test_missing_data_is_unknown_never_safe():
    checks, bonus = evaluate_solana(None, parse_holder_profile(profile()), S)
    assert verdict_of(checks)[0] == "UNKNOWN"
    checks, bonus = evaluate_solana(parse_mint_account(rpc_mint()), None, S)
    assert verdict_of(checks)[0] == "UNKNOWN"
    # a hard FAIL still wins over missing data
    checks, _ = evaluate_solana(parse_mint_account(rpc_mint(mint_auth="X")), None, S)
    assert verdict_of(checks)[0] == "UNSAFE"


# ---- checker with sources + cache ------------------------------------------------------------

class FakeClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = 0

    async def token_holder_profile(self, chain, address):
        self.calls += 1
        if self.error:
            raise self.error
        return self.payload

    async def token_top_traders(self, chain, address, **kw):
        return []


class FakeRpcChecker(SafetyChecker):
    def __init__(self, *a, rpc_payload=None, **kw):
        super().__init__(*a, **kw)
        self.rpc_payload = rpc_payload
        self.rpc_calls = 0

    async def _rpc(self, url, method, params):
        self.rpc_calls += 1
        return self.rpc_payload


class Clock:
    def __init__(self, t=NOW):
        self.t = float(t)

    def __call__(self):
        return self.t


@pytest.mark.asyncio
async def test_checker_combines_sources_caches_and_persists(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    clk = Clock()
    ch = FakeRpcChecker(conn, FakeClient(profile(top10=18, dev=0.5)), CULedger(conn), SCFG, 100_000,
                        {"solana": "http://rpc"}, clock=clk, rpc_payload=rpc_mint())
    r = await ch.check(SOL, "TOK")
    assert r.verdict == "SAFE" and r.bonus >= 3 and not r.from_cache and ch.calls == 1 and ch.cu_today == 25
    assert r.sources["mint"]["program"] == "spl-token" and r.sources["profile"]["top10_pct"] == 18
    r2 = await ch.check(SOL, "TOK")
    assert r2.from_cache and r2.verdict == "SAFE" and r2.bonus == r.bonus and ch.calls == 1 and ch.rpc_calls == 1
    row = conn.execute("SELECT verdict, checked_ts FROM safety WHERE address='TOK'").fetchone()
    assert row["verdict"] == "SAFE" and row["checked_ts"] == NOW
    clk.t += SCFG["cache_s"] + 1
    r3 = await ch.check(SOL, "TOK")
    assert not r3.from_cache and ch.calls == 2
    r4 = await ch.check(SOL, "TOK", force=True)
    assert not r4.from_cache and ch.calls == 3


@pytest.mark.asyncio
async def test_checker_degrades_to_unknown_on_failures_and_never_bonuses_unsafe(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    ch = FakeRpcChecker(conn, FakeClient(error=BirdeyeError("x", 500, "down")), CULedger(conn), SCFG, 100_000,
                        {"solana": "http://rpc"}, rpc_payload=rpc_mint())
    r = await ch.check(SOL, "TOK")
    assert r.verdict == "UNKNOWN" and "top10" in r.reasons and r.bonus == 0
    ch2 = FakeRpcChecker(conn, FakeClient(profile(top10=10, dev=0.1, bundler=1, smart_trader=5)), CULedger(conn), SCFG,
                         100_000, {"solana": "http://rpc"}, rpc_payload=rpc_mint(mint_auth="X"))
    r2 = await ch2.check(SOL, "TOK2")
    assert r2.verdict == "UNSAFE" and r2.bonus == 0 and r2.reasons == ["mint_authority"]


@pytest.mark.asyncio
async def test_budget_guard_skips_holder_profile(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    ch = FakeRpcChecker(conn, FakeClient(profile()), CULedger(conn), {**SCFG, "daily_cu_budget": 10}, 100_000,
                        {"solana": "http://rpc"}, rpc_payload=rpc_mint())
    r = await ch.check(SOL, "TOK")
    assert ch.calls == 0 and r.verdict == "UNKNOWN"


@pytest.mark.asyncio
async def test_evm_without_rpc_data_is_unknown(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    ch = FakeRpcChecker(conn, FakeClient(profile()), CULedger(conn), SCFG, 100_000, {"solana": "http://rpc", "robinhood": "http://rh"})
    r = await ch.check(RH, "0xT")          # every RPC answer is None -> sim cannot run -> UNKNOWN, never SAFE
    assert r.verdict == "UNKNOWN" and "honeypot_sim" in r.reasons and "top10_unknown" in r.flags
