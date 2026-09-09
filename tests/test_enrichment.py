import json
from pathlib import Path

import pytest

from scanner.birdeye import BirdeyeError
from scanner.config import ChainConfig
from scanner.db import column_names, open_db
from scanner.enrichment import Enricher
from scanner.ledger import CULedger
from scanner.stage0 import ROW_COLUMNS, TokenRow
from scanner.wash import parse_tag_flows

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
ECFG = CFG["stage2"]["enrichment"]
NOW = 1_788_866_000
SOL = ChainConfig("solana", True, "solana", 60, {}, {})
RH = ChainConfig("robinhood", True, "robinhood", 120, {}, {})


class FakeClient:
    def __init__(self, traders=None, tags=None, error=None):
        self.traders = traders or []
        self.tags = tags or {}
        self.error = error
        self.calls: list[str] = []

    async def token_top_traders(self, chain, address, **kw):
        self.calls.append("top_traders")
        if self.error:
            raise self.error
        return list(self.traders)

    async def wallet_tags_tracker(self, chain, address, **kw):
        self.calls.append("tags")
        if self.error:
            raise self.error
        return dict(self.tags)


class Clock:
    def __init__(self, t=NOW):
        self.t = float(t)

    def __call__(self):
        return self.t


def seed_row(conn, chain="solana", address="TOK", mcap=1_000_000.0, price=0.01):
    row = TokenRow(chain=chain, cycle_id=1, ts=NOW, address=address, sort_key="x", rank=0, symbol="T",
                   price=price, liquidity=50_000.0, market_cap=mcap)
    conn.execute(f"INSERT INTO scan_rows({', '.join(ROW_COLUMNS)}) VALUES({', '.join('?' * len(ROW_COLUMNS))})",
                 tuple(getattr(row, c) for c in ROW_COLUMNS))


def test_schema_v5_columns_and_table(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    cols = column_names(conn, "tape_features")
    for c in ("wash_score", "hard_vetoes", "soft_flags", "wash_json"):
        assert c in cols
    assert "enrichment" in {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.mark.asyncio
async def test_holdings_uses_supply_from_scan_row_and_caches(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    seed_row(conn, mcap=1_000_000.0, price=0.01)          # supply = 100,000,000
    client = FakeClient(traders=[{"owner": "W1", "holdVolume": 5_000_000}, {"owner": "W2", "holdVolume": 0}])
    clk = Clock()
    e = Enricher(conn, client, CULedger(conn), ECFG, daily_cu_cap=100_000, clock=clk)
    h = await e.holdings(SOL, "TOK")
    assert h == {"W1": 5.0, "W2": 0.0} and e.calls == 1 and e.cu_today == 25
    h2 = await e.holdings(SOL, "TOK")                      # cached
    assert h2 == h and e.calls == 1 and e.cache_hits == 1 and len(client.calls) == 1
    clk.t += ECFG["holdings_cache_s"] + 1                   # cache expired -> refetch
    await e.holdings(SOL, "TOK")
    assert e.calls == 2


@pytest.mark.asyncio
async def test_holdings_without_supply_is_empty_and_errors_are_none(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    e = Enricher(conn, FakeClient(traders=[{"owner": "W1", "holdVolume": 1}]), CULedger(conn), ECFG, 100_000)
    assert await e.holdings(SOL, "NOROW") == {}             # no scan row -> no supply -> {}
    e2 = Enricher(conn, FakeClient(error=BirdeyeError("x", 500, "down")), CULedger(conn), ECFG, 100_000)
    assert await e2.holdings(SOL, "TOK") is None


@pytest.mark.asyncio
async def test_tag_flows_solana_only_parsed_and_cached(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    payload = {"groups": {"tags": {"dev": [{"volume_sell_usd": 500, "wallet_sell_count": 1}],
                                   "smart_trader": [{"volume_buy_usd": 900}, {"volume_buy_usd": 100}]}}}
    client = FakeClient(tags=payload)
    e = Enricher(conn, client, CULedger(conn), ECFG, 100_000)
    flows = await e.tag_flows(SOL, "TOK", now=NOW)
    assert flows["dev"]["sell_usd"] == 500 and flows["smart_trader"]["buy_usd"] == 1000
    assert e.cu_today == 30 and await e.tag_flows(SOL, "TOK", now=NOW) == flows and e.calls == 1
    assert await e.tag_flows(RH, "0xT", now=NOW) is None and len(client.calls) == 1   # EVM: no call


@pytest.mark.asyncio
async def test_enrichment_budget_guard(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    seed_row(conn)
    e = Enricher(conn, FakeClient(traders=[]), CULedger(conn), {**ECFG, "daily_cu_budget": 20}, 100_000)
    assert await e.holdings(SOL, "TOK") is None and e.budget_skips == 1


def test_parse_documented_nested_shape():
    payload = {"groups": {"tags": {"kol": [{"volume_buy_usd": 1, "volume_sell_usd": 2}]},
                          "tag_combinations": {"dev_sniper": [{"volume_sell_usd": 999}]},
                          "top_10_holder": [{"volume_sell_usd": 50}]}}
    f = parse_tag_flows(payload)
    assert f["kol"]["sell_usd"] == 2 and f["top_10_holder"]["sell_usd"] == 50
    assert "dev_sniper" not in f        # combinations are not summed into tags
