import json
from pathlib import Path

import pytest

from scanner.birdeye import BirdeyeError
from scanner.config import ChainConfig
from scanner.db import open_db
from scanner.ledger import CULedger
from scanner.tape import TapePoller, TapeStore, TokenTape, fetch_tape, make_sig, normalize

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
TOKEN = "TokenMint111111111111111111111111111111111"
SOL = ChainConfig("solana", True, "solana", 60, {}, {})
RH = ChainConfig("robinhood", True, "robinhood", 120, {}, {})


def sol_item(i: int, ts: int, side="buy", usd=10.0, owner=None, tx=None, ins=0, inner=0, price=2.0, amt=5.0):
    """Shape observed live 2026-09-09 (Solana, pump_amm)."""
    return {"tx_hash": tx or f"sig{i}", "ins_index": ins, "inner_ins_index": inner, "block_unix_time": ts,
            "block_number": 1000 + i, "side": side, "tx_type": side, "owner": owner or f"w{i}",
            "volume_usd": usd, "volume": amt, "source": "pump_amm", "price_pair": 1e-8,
            "from": {"symbol": "TOK", "address": TOKEN, "price": price, "ui_amount": amt, "ui_change_amount": -amt}
                    if side == "sell" else {"symbol": "SOL", "address": "So111", "price": 100.0, "ui_amount": 0.05},
            "to": {"symbol": "SOL", "address": "So111", "price": 100.0, "ui_amount": 0.05}
                  if side == "sell" else {"symbol": "TOK", "address": TOKEN, "price": price, "ui_amount": amt, "ui_change_amount": amt}}


def rh_item(i: int, ts: int, log_index: int, tx="0xabc", side="buy", usd=50.0):
    """Shape observed live (Robinhood): log_index instead of inner_ins_index; several legs per tx."""
    return {"tx_hash": tx, "ins_index": 16, "log_index": log_index, "block_unix_time": ts, "block_number": 5,
            "side": side, "tx_type": side, "owner": f"0xw{i}", "volume_usd": usd, "volume": 1.0, "source": "0xpool",
            "price_pair": 0.05, "from": {"symbol": "GME", "address": "0x1b0e", "price": 19.0, "ui_amount": 8.5},
            "to": {"symbol": "USDG", "address": "0xTOKEN", "price": 1.0, "ui_amount": usd, "ui_change_amount": usd}}


class FakeClient:
    def __init__(self, pages: list[list[dict]], has_next_last=False, error: Exception | None = None):
        self.pages = pages
        self.has_next_last = has_next_last
        self.error = error
        self.calls: list[tuple[str, str, int, int]] = []

    async def txs_token_v3(self, chain, address, limit=100, offset=0, **kw):
        self.calls.append((chain, address, limit, offset))
        if self.error:
            raise self.error
        idx = offset // limit
        if idx >= len(self.pages):
            return [], False
        return list(self.pages[idx]), (idx < len(self.pages) - 1) or self.has_next_last


# ---- normalisation ----------------------------------------------------------------

def test_sig_includes_instruction_indexes_solana_and_log_index_evm():
    assert make_sig(sol_item(1, 100, tx="h", ins=5, inner=0)) == "h:5:0"
    assert make_sig(rh_item(1, 100, log_index=3, tx="0xh")) == "0xh:16:3"
    # two legs of one EVM tx must NOT collide
    assert make_sig(rh_item(1, 100, 3)) != make_sig(rh_item(2, 100, 7))


def test_normalize_picks_the_token_leg_for_price_and_amount():
    t = normalize(sol_item(1, 100, side="buy", price=2.5, amt=40.0, usd=100.0), "solana", TOKEN)
    assert t.side == "buy" and t.price == 2.5 and t.amount == 40.0 and t.usd == 100.0 and t.wallet == "w1"
    s = normalize(sol_item(2, 101, side="sell", price=2.4, amt=7.0), "solana", TOKEN)
    assert s.side == "sell" and s.price == 2.4 and s.amount == 7.0
    e = normalize(rh_item(1, 100, 3), "robinhood", "0xtoken")   # case-insensitive EVM match
    assert e.price == 1.0 and e.amount == 50.0 and e.side == "buy"


def test_normalize_rejects_incomplete_items():
    assert normalize({"tx_hash": "x"}, "solana", TOKEN) is None
    assert normalize({"tx_hash": "x", "block_unix_time": 1, "side": "swap"}, "solana", TOKEN) is None
    bad = sol_item(1, 100); bad["volume_usd"] = "n/a"
    assert normalize(bad, "solana", TOKEN).usd is None


# ---- ring --------------------------------------------------------------------------

def test_ring_dedupes_sorts_and_caps():
    tape = TokenTape("solana", TOKEN, ring_size=5)
    a = [normalize(sol_item(i, 100 + i), "solana", TOKEN) for i in (3, 1, 2)]
    assert len(tape.add(a)) == 3
    assert [t.ts for t in tape.trades()] == [101, 102, 103]
    assert tape.add(a) == []                                    # all known
    more = [normalize(sol_item(i, 100 + i), "solana", TOKEN) for i in range(4, 10)]
    tape.add(more)
    assert len(tape) == 5 and tape.oldest_ts == 105 and tape.newest_ts == 109   # oldest evicted
    assert tape.known("sig9:0:0") and not tape.known("sig1:0:0")


def test_ring_stats():
    tape = TokenTape("solana", TOKEN)
    tape.add([normalize(sol_item(1, 100, "buy", 10, owner="A"), "solana", TOKEN),
              normalize(sol_item(2, 200, "sell", 30, owner="B"), "solana", TOKEN),
              normalize(sol_item(3, 300, "buy", 50, owner="A"), "solana", TOKEN)])
    assert tape.sum_usd(150) == 80 and tape.sum_usd(150, "buy") == 50 and tape.count(0) == 3
    assert tape.unique_wallets(0) == 2 and tape.unique_wallets(0, "buy") == 1
    assert [t.ts for t in tape.trades(last_n=2)] == [200, 300]


# ---- store / persistence -----------------------------------------------------------------

def test_store_persists_new_trades_and_reloads_on_restart(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    store = TapeStore(conn)
    new, dup = store.ingest("solana", TOKEN, [sol_item(i, 100 + i) for i in range(3)] + [sol_item(1, 101)])
    assert (new, dup) == (3, 1)
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 3
    store2 = TapeStore(conn)                     # "restart"
    tape = store2.get("solana", TOKEN)
    assert len(tape) == 3 and tape.known("sig0:0:0")
    new, dup = store2.ingest("solana", TOKEN, [sol_item(0, 100)])   # already in DB -> dup
    assert (new, dup) == (0, 1)


def test_store_counts_invalid_items_as_skipped(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    store = TapeStore(conn)
    assert store.ingest("solana", TOKEN, [sol_item(1, 100), {"garbage": True}]) == (1, 1)


# ---- fetch -------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_pages_until_has_next_false(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    store = TapeStore(conn)
    pages = [[sol_item(i, 1000 - i) for i in range(100)], [sol_item(i, 1000 - i) for i in range(100, 150)]]
    client = FakeClient(pages)
    r = await fetch_tape(client, store, SOL, TOKEN, max_pages=5)
    assert r.pages == 2 and r.fetched == 150 and r.new == 150 and r.cu == 24 and r.stopped == "has_next=false"
    assert [c[3] for c in client.calls] == [0, 100]


@pytest.mark.asyncio
async def test_fetch_stops_at_page_cap_then_incremental_stops_at_known(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    store = TapeStore(conn)
    pages = [[sol_item(i, 1000 - i) for i in range(100)], [sol_item(i, 1000 - i) for i in range(100, 200)],
             [sol_item(i, 1000 - i) for i in range(200, 300)]]
    r = await fetch_tape(FakeClient(pages, has_next_last=True), store, SOL, TOKEN, max_pages=2)
    assert r.pages == 2 and r.new == 200 and r.stopped == "page_cap"
    # incremental: 10 new trades arrived; page 1 = 10 new + 90 known -> page 2 is all known -> stop
    newer = [sol_item(1000 + i, 2000 - i) for i in range(10)]
    pages2 = [newer + pages[0][:90], pages[0][90:] + pages[1][:90], pages[1][90:] + pages[2]]
    client = FakeClient(pages2, has_next_last=True)
    r2 = await fetch_tape(client, store, SOL, TOKEN, max_pages=5)
    assert r2.new == 10 and r2.pages == 2 and r2.stopped == "reached_known"


@pytest.mark.asyncio
async def test_fetch_reports_api_error(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    r = await fetch_tape(FakeClient([], error=BirdeyeError("txs_token_v3", 500, "boom")), TapeStore(conn), SOL, TOKEN)
    assert r.error and r.stopped == "error" and r.pages == 0


# ---- poller -------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_poller_first_fetch_depth_refresh_cadence_and_budget(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    store = TapeStore(conn)
    pages = [[sol_item(i, 1000 - i) for i in range(100)], [sol_item(i, 1000 - i) for i in range(100, 200)]]
    client = FakeClient(pages, has_next_last=True)
    settings = {"poll_interval_s": 60, "pages_on_first_fetch": 2, "refresh_every_n_polls": 3, "daily_cu_budget": 60,
                "first_contact_min_span_s": 0}          # cadence/budget test: no first-contact depth
    poller = TapePoller(client, store, CULedger(conn), settings, daily_cu_cap=10_000, clock=lambda: 1_788_866_000.0)
    active = [(SOL, TOKEN)]
    s1 = await poller.poll(active)                      # first: 2 pages = 24 CU
    assert s1.polled == 1 and s1.cu == 24 and len(client.calls) == 2
    s2 = await poller.poll(active); s3 = await poller.poll(active)   # polls 1,2 -> skipped by cadence
    assert s2.skipped_refresh == 1 and s3.skipped_refresh == 1 and len(client.calls) == 2
    s4 = await poller.poll(active)                      # poll 3 -> refresh, 1 page = 12 CU (total 36)
    assert s4.polled == 1 and s4.cu == 12 and poller.cu_today == 36
    await poller.poll(active); await poller.poll(active)
    s7 = await poller.poll(active)                      # 36 + 12 = 48 <= 60 ok
    assert s7.polled == 1 and poller.cu_today == 48
    await poller.poll(active); await poller.poll(active)
    s10 = await poller.poll(active)                     # 48 + 12 = 60 <= 60 ok
    assert s10.polled == 1 and poller.cu_today == 60
    await poller.poll(active); await poller.poll(active)
    s13 = await poller.poll(active)                     # 72 > 60 -> budget skip
    assert s13.skipped_budget == 1 and s13.polled == 0


@pytest.mark.asyncio
async def test_poller_day_rollover_resets_budget(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    t = {"now": 1_788_866_000.0}
    poller = TapePoller(FakeClient([[sol_item(0, 100)]]), TapeStore(conn), None,
                        {"daily_cu_budget": 12, "pages_on_first_fetch": 1}, 10_000, clock=lambda: t["now"])
    await poller.poll([(SOL, TOKEN)])
    assert poller.cu_today == 12
    t["now"] += 86400
    assert poller._budget_ok(12) and poller.cu_today == 0


@pytest.mark.asyncio
async def test_first_contact_depth_pages_until_span_covers_baseline(tmp_path):
    """A hot token: 100 trades per 60 s. Two pages span 2 min; the poller must keep paging on first
    contact until the tape spans >= first_contact_min_span_s (600 s) or the page cap."""
    conn = open_db(tmp_path / "t.sqlite")
    store = TapeStore(conn)
    t_now = 1_788_866_000
    pages = [[sol_item(p * 100 + i, t_now - (p * 100 + i) * 0.6) for i in range(100)] for p in range(8)]
    for pg in pages:                      # int timestamps
        for it in pg:
            it["block_unix_time"] = int(it["block_unix_time"])
    client = FakeClient(pages, has_next_last=True)
    settings = {"pages_on_first_fetch": 2, "refresh_every_n_polls": 3, "daily_cu_budget": 10_000,
                "first_contact_min_span_s": 600, "first_contact_max_pages": 6}
    poller = TapePoller(client, store, CULedger(conn), settings, daily_cu_cap=100_000, clock=lambda: float(t_now))
    st = await poller.poll([(SOL, TOKEN)])
    tape = store.get(SOL.name, TOKEN)
    # 100 trades/min -> 600 s needs ~10 pages, capped at 6 -> 600 trades, span ~360 s
    assert len(client.calls) == 6 and st.fetched == 600 and poller.deep_fetches == 4
    assert tape.polls == 1                                   # still ONE first contact
    assert [c[3] for c in client.calls] == [0, 100, 200, 300, 400, 500]
    # a quiet token stops after the initial pages
    conn2 = open_db(tmp_path / "q.sqlite")
    quiet = [[sol_item(i, t_now - i * 30) for i in range(100)], [sol_item(100 + i, t_now - (100 + i) * 30) for i in range(100)]]
    c2 = FakeClient(quiet, has_next_last=True)
    p2 = TapePoller(c2, TapeStore(conn2), CULedger(conn2), settings, 100_000, clock=lambda: float(t_now))
    await p2.poll([(SOL, TOKEN)])
    assert len(c2.calls) == 2 and p2.deep_fetches == 0


def test_store_loads_db_history_even_if_first_touched_without_loading(tmp_path):
    """Milestone C reproducibility finding: the runner peeked at a tape with load_from_db=False, which cached an
    empty ring; the DB history was then never loaded and live evaluated on fewer trades than the replay."""
    conn = open_db(tmp_path / "t.sqlite")
    store = TapeStore(conn)
    store.ingest("solana", TOKEN, [sol_item(i, 1000 - i) for i in range(50)])
    store.drop("solana", TOKEN)                                   # process restart / candidate expiry
    assert store.polls_of("solana", TOKEN) == 0 and ("solana", TOKEN) not in store.tapes   # peek creates nothing
    t0 = store.get("solana", TOKEN, load_from_db=False)
    assert len(t0) == 0 and not t0.loaded_from_db
    t1 = store.get("solana", TOKEN)                               # the poller's normal get -> history arrives
    assert t1 is t0 and len(t1) == 50 and t1.loaded_from_db
    assert len(store.get("solana", TOKEN)) == 50                  # loaded once, not re-added
