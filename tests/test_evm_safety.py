import json
from pathlib import Path

import pytest

from scanner.config import ChainConfig
from scanner.db import open_db
from scanner.evm_safety import (SimReport, PathResult, calldata_balance_of, calldata_transfer, evaluate_evm,
                                parse_sim_block, read_owner, simulate_paths, top10_proxy_pct, _settings)
from scanner.ledger import CULedger
from scanner.safety import SafetyChecker, verdict_of, Check

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
S = _settings(CFG["safety"]["evm"])
RH = ChainConfig("robinhood", True, "robinhood", 120, {}, {})
TOKEN = "0x0339f5459FC690aC85F1782e15782A151b4A9E1b"
PM = S["pool_manager"]["robinhood"]
HOLDER = "0x56Ba45beC8EB91Be8382b22946c5F709DB113c39"


def u(v: int) -> str:
    return "0x" + hex(v)[2:].rjust(64, "0")


def call(ok=True, ret=None, err=None):
    c = {"status": "0x1" if ok else "0x0", "returnData": ret or "0x", "logs": [], "gasUsed": "0x1"}
    if not ok:
        c["error"] = {"code": -32000, "message": err or "execution reverted"}
    return c


def block(pre, ok, post, err=None):
    return {"calls": [call(True, u(pre)), call(ok, "0x" + "0" * 63 + "1", err), call(True, u(post))]}


class ScriptedRpc:
    """Answers eth_simulateV1 with scripted blocks; records every request."""

    def __init__(self, sim_blocks=None, sim_error=None):
        self.sim_blocks = sim_blocks
        self.sim_error = sim_error
        self.calls: list[tuple[str, list]] = []

    async def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "eth_simulateV1":
            if self.sim_error:
                return {"error": {"code": -32000, "message": self.sim_error}}
            return {"result": self.sim_blocks}
        return None


def test_calldata_encoding():
    assert calldata_transfer(PM, 1000) == "0xa9059cbb" + PM[2:].lower().rjust(64, "0") + hex(1000)[2:].rjust(64, "0")
    assert calldata_balance_of(HOLDER).startswith("0x70a08231") and len(calldata_balance_of(HOLDER)) == 10 + 64


def test_parse_sim_block_pads_and_reads_errors():
    got = parse_sim_block({"calls": [call(True, "0x01"), call(False, err="boom")]}, 3)
    assert got[0] == (True, "0x01", None) and got[1] == (False, "0x", "boom") and got[2] == (False, None, "missing")
    assert parse_sim_block(None, 2) == [(False, None, "missing")] * 2


@pytest.mark.asyncio
async def test_clean_token_all_paths_pass_with_zero_tax():
    # holder sells 1% of 100 units = 1 unit; pool pre 50 -> 51; pool sends 1% of 50 = 0.5 to fresh; transfer half of that
    hb, cpb = 100 * 10**18, 50 * 10**18
    sell = int(hb * 0.01); buy = int(cpb * 0.01); xfer = buy // 2
    rpc = ScriptedRpc([block(cpb, True, cpb + sell), block(0, True, buy), block(0, True, xfer)])
    rep = await simulate_paths(rpc, TOKEN, HOLDER, hb, PM, cpb, S)
    assert rep.error is None and [p.name for p in rep.paths] == ["sell", "buy", "transfer"]
    assert all(p.ok and p.tax_pct == 0.0 for p in rep.paths)
    m, params = rpc.calls[0]
    assert m == "eth_simulateV1" and params[0]["validation"] is False
    blocks = params[0]["blockStateCalls"]
    assert blocks[0]["calls"][1]["from"] == HOLDER and blocks[1]["calls"][1]["from"] == PM
    assert HOLDER in blocks[0]["stateOverrides"] and PM in blocks[0]["stateOverrides"]
    checks, bonus = evaluate_evm(rep, "renounced", 8.0, S)
    v, reasons, flags = verdict_of([Check(c.name, c.passed, c.value, c.limit, c.hard) for c in checks])
    assert v == "SAFE" and reasons == [] and bonus == 4.0 and "top10_unverified" in flags


@pytest.mark.asyncio
async def test_honeypot_sell_revert_is_unsafe_and_taxes_are_measured():
    hb, cpb = 100 * 10**18, 50 * 10**18
    sell = int(hb * 0.01); buy = int(cpb * 0.01); xfer = buy // 2
    rpc = ScriptedRpc([block(cpb, False, cpb, err="transfer blocked"), block(0, True, int(buy * 0.9)), block(0, True, xfer)])
    rep = await simulate_paths(rpc, TOKEN, HOLDER, hb, PM, cpb, S)
    sell_p, buy_p = rep.path("sell"), rep.path("buy")
    assert sell_p.ok is False and "blocked" in sell_p.error
    assert buy_p.ok and buy_p.tax_pct == pytest.approx(10.0)     # 10% skimmed on the buy path
    checks, bonus = evaluate_evm(rep, "active", None, S)
    v, reasons, flags = verdict_of([Check(c.name, c.passed, c.value, c.limit, c.hard) for c in checks])
    assert v == "UNSAFE" and "sell_path" in reasons and "buy_tax" in reasons and "owner" in flags and bonus == 0


@pytest.mark.asyncio
async def test_fallback_when_no_holder_uses_pool_roundtrip():
    cpb = 50 * 10**18
    buy = int(cpb * 0.01); xfer = buy // 2; sell_back = buy // 4
    rpc = ScriptedRpc([block(0, True, buy), block(0, True, xfer), block(cpb, True, cpb + sell_back)])
    rep = await simulate_paths(rpc, TOKEN, None, None, PM, cpb, S)
    assert [p.name for p in rep.paths] == ["buy", "transfer", "sell"] and all(p.ok for p in rep.paths)
    blocks = rpc.calls[0][1][0]["blockStateCalls"]
    assert blocks[0]["calls"][1]["from"] == PM and blocks[2]["calls"][1]["to"] == TOKEN


@pytest.mark.asyncio
async def test_sim_error_and_empty_pool_are_unknown():
    rep = await simulate_paths(ScriptedRpc(sim_error="method not found"), TOKEN, HOLDER, 10**18, PM, 10**18, S)
    assert rep.error == "method not found" and rep.paths == []
    checks, _ = evaluate_evm(rep, "none", None, S)
    assert verdict_of([Check(c.name, c.passed, c.value, c.limit, c.hard) for c in checks])[0] == "UNKNOWN"
    # a clean sim with NO concentration data is SAFE with a soft flag (proxy absence must not cap Robinhood)
    ok = SimReport(holder=HOLDER, counterparty=PM, paths=[PathResult(n, True, 1, 1, 0.0) for n in ("sell", "buy", "transfer")])
    c2, b2 = evaluate_evm(ok, "none", None, S)
    v2, r2, f2 = verdict_of([Check(c.name, c.passed, c.value, c.limit, c.hard) for c in c2])
    assert v2 == "SAFE" and r2 == [] and "top10_unknown" in f2 and b2 == 3.0
    rep2 = await simulate_paths(ScriptedRpc([]), TOKEN, HOLDER, 10**18, PM, 0, S)
    assert "no tokens" in rep2.error


@pytest.mark.asyncio
async def test_read_owner_states():
    class R:
        def __init__(self, res): self.res = res
        async def __call__(self, m, p): return self.res
    assert await read_owner(R({"error": {"message": "execution reverted"}}), TOKEN) == ("none", None)
    assert await read_owner(R({"result": u(0)}), TOKEN) == ("renounced", "0x" + "0" * 40)
    st, addr = await read_owner(R({"result": "0x" + HOLDER[2:].lower().rjust(64, "0")}), TOKEN)
    assert st == "active" and addr == HOLDER.lower()


def test_top10_proxy_is_a_lower_bound_and_can_prove_unsafe():
    supply, dec = 1_000_000_000 * 10**18, 18
    traders = [{"owner": "a", "holdVolume": 300_000_000}, {"owner": "b", "holdVolume": 100_000_000}, {"holdVolume": "x"}]
    assert top10_proxy_pct(traders, supply, dec) == pytest.approx(40.0)
    assert top10_proxy_pct(traders, None, dec) is None and top10_proxy_pct([], supply, dec) is None
    rep = SimReport(holder=HOLDER, counterparty=PM, paths=[PathResult("sell", True, 1, 1, 0.0), PathResult("buy", True, 1, 1, 0.0),
                                                           PathResult("transfer", True, 1, 1, 0.0)])
    checks, _ = evaluate_evm(rep, "none", 40.0, S)
    v, reasons, _ = verdict_of([Check(c.name, c.passed, c.value, c.limit, c.hard) for c in checks])
    assert v == "UNSAFE" and reasons == ["top10_proxy"]


class FakeClient:
    async def token_top_traders(self, chain, address, **kw):
        return [{"owner": "a", "holdVolume": 1_000_000}]


class FakeEvmChecker(SafetyChecker):
    """Routes RPC calls to scripted answers keyed by method/selector."""

    def __init__(self, *a, script, **kw):
        super().__init__(*a, **kw)
        self.script = script
        self.rpc_log = []

    async def _rpc(self, url, method, params):
        self.rpc_log.append(method)
        if method == "eth_call":
            data = params[0]["data"]
            for key, val in self.script.items():
                if data.startswith(key):
                    return val
            return {"result": "0x"}
        return self.script.get("sim")


@pytest.mark.asyncio
async def test_checker_evm_end_to_end_with_tape_holder(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    conn.execute("INSERT INTO trades(chain,address,sig,ts,side,wallet,usd) VALUES('robinhood',?, 's1', 1, 'buy', ?, 500)", (TOKEN, HOLDER))
    hb, cpb = 100 * 10**18, 50 * 10**18
    sell = int(hb * 0.01); buy = int(cpb * 0.01); xfer = buy // 2
    script = {
        "0x18160ddd": {"result": u(1_000_000_000 * 10**18)},                 # totalSupply
        "0x313ce567": {"result": u(18)},                                       # decimals
        "0x8da5cb5b": {"error": {"message": "execution reverted"}},            # owner() -> none
        calldata_balance_of(PM): {"result": u(cpb)},
        calldata_balance_of(HOLDER): {"result": u(hb)},
        "sim": {"result": [block(cpb, True, cpb + sell), block(0, True, buy), block(0, True, xfer)]},
    }
    ch = FakeEvmChecker(conn, FakeClient(), CULedger(conn), CFG["safety"], 100_000, {"robinhood": "http://rpc"}, script=script)
    r = await ch.check(RH, TOKEN)
    assert r.verdict == "SAFE" and r.sources["sim"]["holder"] == HOLDER and r.sources["owner_state"] == "none"
    assert r.sources["top10_proxy_pct"] == pytest.approx(0.1) and r.bonus == 4.0
    assert "eth_simulateV1" in ch.rpc_log and ch.cu_today == 25
    r2 = await ch.check(RH, TOKEN)
    assert r2.from_cache and r2.verdict == "SAFE"
