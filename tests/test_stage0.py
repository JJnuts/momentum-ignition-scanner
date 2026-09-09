import json
from pathlib import Path

import pytest

from scanner.birdeye import BirdeyeError
from scanner.config import ChainConfig
from scanner.db import column_names, open_db
from scanner.ledger import CULedger
from scanner.stage0 import ROW_COLUMNS, Stage0Scanner, TokenRow, build_params, verify_filters

FIXTURES = Path(__file__).parent / "fixtures"
SOL_ITEMS = json.loads((FIXTURES / "list_v3_solana.json").read_text(encoding="utf-8"))
RH_ITEMS = json.loads((FIXTURES / "list_v3_robinhood.json").read_text(encoding="utf-8"))


NOW = 1_788_866_000


def good(item: dict, **over) -> dict:
    """Copy of a real fixture item forced to satisfy the default Stage-0 filters at NOW."""
    d = dict(item)
    d.update({"liquidity": 1_000_000.0, "market_cap": 5_000_000.0, "holder": 1000,
              "last_trade_unix_time": NOW, "volume_5m_usd": 100_000.0, "volume_1h_usd": 500_000.0})
    d.update(over)
    return d


def chain_cfg(name="solana", **stage0) -> ChainConfig:
    base = {"filters": {"min_liquidity": 8000, "min_market_cap": 20000, "min_holder": 30, "min_volume_5m_usd": 1500},
            "alive_within_s": 90, "page_limit": 100, "sort_keys": ["volume_5m_change_percent", "volume_1m_usd"]}
    base.update(stage0)
    return ChainConfig(name=name, enabled=True, birdeye_chain=name, scan_interval_s=60, stage0=base, stage1={})


class FakeClient:
    def __init__(self, items=None, error: Exception | None = None):
        self.items = items if items is not None else [good(i) for i in SOL_ITEMS]
        self.error = error
        self.calls: list[dict] = []

    async def token_list_v3(self, chain, **kw):
        self.calls.append({"chain": chain, **kw})
        if self.error:
            raise self.error
        return list(self.items)


def make_scanner(tmp_path, client, daily_cap=10_000, clock=lambda: 1_788_866_000):
    conn = open_db(tmp_path / "t.sqlite")
    return Stage0Scanner(conn, client, CULedger(conn), daily_cu_cap=daily_cap, clock=clock), conn


# ---- TokenRow --------------------------------------------------------------

def test_row_columns_match_schema(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    cols = set(column_names(conn, "scan_rows"))
    assert set(ROW_COLUMNS) <= cols, set(ROW_COLUMNS) - cols


def test_from_birdeye_solana_real_shape():
    row = TokenRow.from_birdeye(SOL_ITEMS[0], "solana", 1, 1_788_866_000, "liquidity", 0)
    assert row.address and row.symbol
    assert row.liquidity > 0 and row.market_cap > 0 and row.holder > 0
    assert row.vol_5m is not None and row.tr_5m is not None and row.pc_5m is not None
    assert row.vol_24h is not None and row.uw_24h is not None
    assert row.age_s is None if row.listing_ts is None else row.age_s >= 0


def test_from_birdeye_robinhood_missing_short_windows_become_none():
    row = TokenRow.from_birdeye(RH_ITEMS[0], "robinhood", 1, 1_788_866_000, "liquidity", 0)
    assert row.address.startswith("0x")
    assert row.vol_5m is None and row.tr_5m is None and row.pc_5m is None and row.vol_1m is None
    assert row.vol_1h is not None and row.tr_1h is not None and row.vol_24h is not None


def test_from_birdeye_bad_values_are_none():
    item = {"address": "A", "liquidity": "not-a-number", "holder": None, "volume_5m_usd": "12.5"}
    row = TokenRow.from_birdeye(item, "solana", 1, 0, "x", 0)
    assert row.liquidity is None and row.holder is None and row.vol_5m == 12.5


# ---- filters ----------------------------------------------------------------

def test_build_params_adds_alive_time():
    p = build_params({"filters": {"min_liquidity": 5}, "alive_within_s": 90}, now=1000)
    assert p == {"min_liquidity": 5, "min_last_trade_unix_time": 910}
    assert build_params({}, now=1000) == {}


def test_verify_filters_detects_each_kind():
    row = TokenRow.from_birdeye({"address": "A", "liquidity": 100, "market_cap": 5000, "holder": 10,
                                 "last_trade_unix_time": 900, "volume_5m_usd": 50}, "solana", 1, 1000, "x", 0)
    params = {"min_liquidity": 8000, "min_market_cap": 1000, "max_market_cap": 6000, "min_holder": 30,
              "min_last_trade_unix_time": 910, "min_volume_5m_usd": 10, "min_global_fees_paid": 1}
    bad = {k for k, _, _ in verify_filters(row, params)}
    assert bad == {"min_liquidity", "min_holder", "min_last_trade_unix_time"}


def test_verify_filters_none_value_is_a_violation():
    row = TokenRow.from_birdeye({"address": "A"}, "robinhood", 1, 1000, "x", 0)
    assert [k for k, _, _ in verify_filters(row, {"min_volume_5m_usd": 1})] == ["min_volume_5m_usd"]


# ---- scanner cycle ----------------------------------------------------------

@pytest.mark.asyncio
async def test_cycle_persists_rows_and_passes_server_params(tmp_path):
    client = FakeClient()
    scanner, conn = make_scanner(tmp_path, client)
    res = await scanner.cycle(chain_cfg())
    assert res.error is None and res.skipped is None
    assert res.fetched == 3 and res.persisted == 3 and res.violations == 0 and res.cu == 50
    call = client.calls[0]
    assert call["chain"] == "solana" and call["sort_by"] == "volume_5m_change_percent"
    assert call["min_liquidity"] == 8000 and call["min_last_trade_unix_time"] == 1_788_866_000 - 90
    rows = conn.execute("SELECT chain, cycle_id, rank, sort_key, address FROM scan_rows ORDER BY rank").fetchall()
    assert [r["rank"] for r in rows] == [0, 1, 2]
    assert rows[0]["chain"] == "solana" and rows[0]["cycle_id"] == 1 and rows[0]["sort_key"] == "volume_5m_change_percent"


@pytest.mark.asyncio
async def test_sort_keys_alternate_and_cycle_id_survives_restart(tmp_path):
    client = FakeClient()
    scanner, conn = make_scanner(tmp_path, client)
    r1 = await scanner.cycle(chain_cfg())
    r2 = await scanner.cycle(chain_cfg())
    assert (r1.cycle_id, r2.cycle_id) == (1, 2)
    assert (r1.sort_key, r2.sort_key) == ("volume_5m_change_percent", "volume_1m_usd")
    # new scanner instance on the same DB continues the sequence
    scanner2 = Stage0Scanner(conn, client, CULedger(conn), daily_cu_cap=10_000, clock=lambda: 1_788_866_000)
    r3 = await scanner2.cycle(chain_cfg())
    assert r3.cycle_id == 3 and r3.sort_key == "volume_5m_change_percent"


@pytest.mark.asyncio
async def test_violating_and_duplicate_rows_are_dropped(tmp_path):
    bad = good(SOL_ITEMS[0], liquidity=1.0)              # violates min_liquidity
    dup = good(SOL_ITEMS[1])                              # duplicate address
    client = FakeClient(items=[good(SOL_ITEMS[1]), dup, bad, good(SOL_ITEMS[2])])
    scanner, conn = make_scanner(tmp_path, client)
    res = await scanner.cycle(chain_cfg())
    assert res.fetched == 4 and res.persisted == 2 and res.violations == 1 and res.duplicates == 1
    assert res.violation_samples[0][1] == "min_liquidity"
    assert conn.execute("SELECT COUNT(*) FROM scan_rows").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_robinhood_rows_pass_1h_filters(tmp_path):
    client = FakeClient(items=[good(i, volume_5m_usd=None) for i in RH_ITEMS])
    cfg = chain_cfg("robinhood", filters={"min_liquidity": 4000, "min_market_cap": 20000, "min_holder": 20,
                                          "min_volume_1h_usd": 2000}, alive_within_s=180,
                     sort_keys=["volume_1h_change_percent"])
    scanner, conn = make_scanner(tmp_path, client)
    res = await scanner.cycle(cfg)
    assert res.persisted == 3 and res.violations == 0
    assert conn.execute("SELECT COUNT(*) FROM scan_rows WHERE vol_5m IS NULL").fetchone()[0] == 3


@pytest.mark.asyncio
async def test_api_error_is_reported_not_raised(tmp_path):
    client = FakeClient(error=BirdeyeError("token_list_v3", 500, "boom"))
    scanner, conn = make_scanner(tmp_path, client)
    res = await scanner.cycle(chain_cfg())
    assert res.error and "boom" in res.error and res.persisted == 0 and res.cu == 0
    assert conn.execute("SELECT COUNT(*) FROM scan_rows").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_budget_guard_skips_cycle_before_calling(tmp_path):
    client = FakeClient()
    scanner, conn = make_scanner(tmp_path, client, daily_cap=40)  # < 50 CU per list call
    res = await scanner.cycle(chain_cfg())
    assert res.skipped == "budget" and client.calls == [] and res.cu == 0
