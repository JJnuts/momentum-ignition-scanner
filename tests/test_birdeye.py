from pathlib import Path

import pytest

from scanner.birdeye import BirdeyeClient, BirdeyeError, EndpointUnavailable, RetryExhausted
from scanner.db import open_db
from scanner.ledger import CULedger
from scanner.ratelimit import TokenBucket
from scanner.recorder import RawRecorder


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s


class FakeTransport:
    """Scripted responses: list of (status, body, headers); records every call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[tuple[str, dict, dict]] = []

    async def __call__(self, path, params, headers):
        self.calls.append((path, dict(params), dict(headers)))
        if not self.script:
            raise AssertionError("transport called more times than scripted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(tmp_path: Path, script, plan="standard", **kw):
    clk = FakeClock()
    conn = open_db(tmp_path / "t.sqlite")
    ledger = CULedger(conn)
    rec = RawRecorder(tmp_path / "raw")
    transport = FakeTransport(script)
    client = BirdeyeClient(api_key="k123", plan=plan, recorder=rec, ledger=ledger, transport=transport,
                           limiter=TokenBucket(1000, clock=clk, sleep=clk.sleep),
                           clock=clk, sleep=clk.sleep, backoff_base_s=1.0, **kw)
    return client, transport, ledger, rec, clk


OK_LIST = (200, {"success": True, "data": {"items": [{"address": "A", "liquidity": 1.0}]}}, {})


@pytest.mark.asyncio
async def test_headers_params_and_data_unwrapping(tmp_path):
    client, tr, ledger, rec, _ = make_client(tmp_path, [OK_LIST])
    items = await client.token_list_v3("solana", sort_by="liquidity", limit=5, min_liquidity=1000, flag=True, skip=None)
    assert items == [{"address": "A", "liquidity": 1.0}]
    path, params, headers = tr.calls[0]
    assert path == "/defi/v3/token/list"
    assert headers["X-API-KEY"] == "k123" and headers["x-chain"] == "solana"
    assert params["min_liquidity"] == 1000 and params["flag"] == "true" and "skip" not in params
    # ledger + recorder
    assert ledger.session_calls == 1 and ledger.session_cu == 50
    recs = list(rec.iter_file(rec.files()[0]))
    assert recs[0]["endpoint"] == "token_list_v3" and recs[0]["status"] == 200


@pytest.mark.asyncio
async def test_plan_gating_blocks_before_http(tmp_path):
    client, tr, ledger, _, _ = make_client(tmp_path, [], plan="standard")
    with pytest.raises(EndpointUnavailable):
        await client.token_security("solana", "A")
    with pytest.raises(EndpointUnavailable):
        await client.trade_data_single("solana", "A")
    assert tr.calls == [] and ledger.session_calls == 0


@pytest.mark.asyncio
async def test_plan_upgrade_unlocks_endpoint(tmp_path):
    body = (200, {"success": True, "data": {"unique_wallet_5m": 7}}, {})
    client, tr, ledger, _, _ = make_client(tmp_path, [body], plan="starter")
    td = await client.trade_data_single("solana", "A", frames=["1m", "5m"])
    assert td["unique_wallet_5m"] == 7
    assert tr.calls[0][1]["frames"] == "1m,5m"
    assert ledger.session_cu == 10


@pytest.mark.asyncio
async def test_429_retries_with_retry_after_then_succeeds(tmp_path):
    script = [(429, {"success": False, "message": "rate"}, {"retry-after": "2.5"}), OK_LIST]
    client, tr, ledger, _, clk = make_client(tmp_path, script)
    t0 = clk.t
    items = await client.token_list_v3("solana")
    assert len(items) == 1
    assert len(tr.calls) == 2 and client.retries == 1
    assert clk.t - t0 == pytest.approx(2.5)
    # failed attempt not charged, successful one charged
    rows = ledger.conn.execute("SELECT status, cu FROM cu_ledger ORDER BY id").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(429, 0), (200, 50)]


@pytest.mark.asyncio
async def test_5xx_exponential_backoff_then_exhausted(tmp_path):
    script = [(503, {"message": "down"}, {})] * 4
    client, tr, _, _, clk = make_client(tmp_path, script, max_retries=3)
    with pytest.raises(RetryExhausted):
        await client.token_list_v3("solana")
    assert len(tr.calls) == 4
    assert clk.t == pytest.approx(1 + 2 + 4)  # backoff 1,2,4 then give up


@pytest.mark.asyncio
async def test_timeout_is_retryable(tmp_path):
    import asyncio
    script = [asyncio.TimeoutError(), OK_LIST]
    client, tr, _, _, _ = make_client(tmp_path, script)
    items = await client.token_list_v3("solana")
    assert len(items) == 1 and len(tr.calls) == 2


@pytest.mark.asyncio
async def test_4xx_is_not_retried(tmp_path):
    script = [(401, {"success": False, "message": "Unauthorized"}, {})]
    client, tr, _, _, _ = make_client(tmp_path, script)
    with pytest.raises(BirdeyeError) as ei:
        await client.token_list_v3("solana")
    assert ei.value.status == 401 and "Unauthorized" in str(ei.value)
    assert len(tr.calls) == 1


@pytest.mark.asyncio
async def test_success_false_body_is_an_error(tmp_path):
    script = [(200, {"success": False, "message": "bad address"}, {})]
    client, _, ledger, _, _ = make_client(tmp_path, script)
    with pytest.raises(BirdeyeError):
        await client.ohlcv_v3("solana", "A", "1m", 0, 60)
    assert ledger.session_cu == 0  # not charged


@pytest.mark.asyncio
async def test_txs_v3_pagination_and_limits(tmp_path):
    body = (200, {"success": True, "data": {"items": [{"tx_hash": "x", "side": "buy"}], "has_next": True}}, {})
    client, tr, ledger, _, _ = make_client(tmp_path, [body])
    items, has_next = await client.txs_token_v3("robinhood", "0xabc", limit=100, after_time=123)
    assert has_next is True and items[0]["side"] == "buy"
    assert tr.calls[0][2]["x-chain"] == "robinhood"
    assert tr.calls[0][1]["after_time"] == 123 and tr.calls[0][1]["sort_type"] == "desc"
    assert ledger.session_cu == 12
    with pytest.raises(ValueError):
        await client.txs_token_v3("solana", "A", limit=101)


@pytest.mark.asyncio
async def test_multi_price_cu_scales_with_batch(tmp_path):
    body = (200, {"success": True, "data": {"A": {"value": 1}, "B": {"value": 2}}}, {})
    client, tr, ledger, _, _ = make_client(tmp_path, [body], plan="lite")
    out = await client.multi_price("solana", ["A", "B"])
    assert out["B"]["value"] == 2
    assert tr.calls[0][1]["list_address"] == "A,B"
    assert ledger.session_cu == 6  # ceil(3 * 2^0.8) = ceil(5.22)


def test_constructor_validation():
    with pytest.raises(ValueError):
        BirdeyeClient(api_key="", plan="standard")
    with pytest.raises(ValueError):
        BirdeyeClient(api_key="k", plan="platinum")
    with pytest.raises(ValueError):
        BirdeyeClient(api_key="k", plan="standard", rate_safety=1.5)


def test_default_limiter_applies_rate_safety_and_no_burst_on_free_tier():
    c = BirdeyeClient(api_key="k", plan="standard")  # 1 rps
    assert c.effective_rps == pytest.approx(0.8)
    assert c.limiter.rate == pytest.approx(0.8) and c.limiter.capacity == 1.0
    c2 = BirdeyeClient(api_key="k", plan="starter", rate_safety=0.9)  # 15 rps
    assert c2.limiter.rate == pytest.approx(13.5) and c2.limiter.capacity == pytest.approx(13.5)
