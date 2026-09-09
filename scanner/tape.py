"""Trade tape - the primitive for Stage 2.

Fetches a token's recent swaps from Birdeye (txs v3, 12 CU per page of <=100),
normalises them, dedupes by (tx_hash, ins_index, inner/log index) - one
transaction can carry several trade legs - keeps an in-memory ring per token
and persists every new trade to `trades`.

TapePoller polls the ACTIVE candidate set (supplied by a callback; T8 owns
the real candidate manager, the runner supplies a provisional one until then)
on a fixed interval, with a first-fetch page depth, a refresh-every-N-polls
policy and a daily CU budget guard, because the tape is the single most
expensive stream in the system.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from .birdeye import BirdeyeClient, BirdeyeError
from .config import ChainConfig
from .ledger import CULedger
from .plans import cu_cost

log = logging.getLogger("tape")


@dataclass(frozen=True)
class Trade:
    chain: str
    address: str      # token address the tape belongs to
    sig: str          # tx_hash:ins_index:inner_or_log_index
    ts: int
    side: str         # buy | sell
    wallet: str | None
    usd: float | None
    price: float | None    # token USD price on this trade
    amount: float | None   # token units (ui amount, absolute)
    source: str | None
    tx_hash: str = ""
    block: int | None = None


def make_sig(item: dict[str, Any]) -> str:
    inner = item.get("inner_ins_index")
    if inner is None:
        inner = item.get("log_index")
    return f"{item.get('tx_hash')}:{item.get('ins_index')}:{inner if inner is not None else ''}"


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def normalize(item: dict[str, Any], chain: str, address: str) -> Trade | None:
    """Birdeye txs v3 item -> Trade. Returns None if the item lacks the essentials."""
    tx_hash = item.get("tx_hash")
    ts = item.get("block_unix_time")
    side = str(item.get("side") or item.get("tx_type") or "").lower()
    if not tx_hash or ts is None or side not in ("buy", "sell"):
        return None
    # the token's own leg: whichever side matches the token address (case-insensitive for EVM)
    leg: dict[str, Any] = {}
    for k in ("from", "to"):
        l = item.get(k) or {}
        if str(l.get("address") or "").lower() == address.lower():
            leg = l
            break
    price = _f(leg.get("price")) if leg else None
    if price is None:
        price = _f(item.get("price_pair"))
    amount = _f(leg.get("ui_amount")) if leg else _f(item.get("volume"))
    return Trade(chain=chain, address=address, sig=make_sig(item), ts=int(ts), side=side,
                 wallet=item.get("owner"), usd=_f(item.get("volume_usd")), price=price,
                 amount=abs(amount) if amount is not None else None, source=item.get("source"),
                 tx_hash=str(tx_hash), block=item.get("block_number"))


class TokenTape:
    """In-memory ring of trades for one token, newest last, deduped by sig."""

    def __init__(self, chain: str, address: str, ring_size: int = 600) -> None:
        self.chain = chain
        self.address = address
        self.ring_size = ring_size
        self._trades: deque[Trade] = deque(maxlen=ring_size)
        self._sigs: set[str] = set()
        self.polls = 0
        self.last_fetch_ts: int | None = None

    def __len__(self) -> int:
        return len(self._trades)

    @property
    def newest_ts(self) -> int | None:
        return self._trades[-1].ts if self._trades else None

    @property
    def oldest_ts(self) -> int | None:
        return self._trades[0].ts if self._trades else None

    def known(self, sig: str) -> bool:
        return sig in self._sigs

    def add(self, trades: Iterable[Trade]) -> list[Trade]:
        """Insert unseen trades, keep the ring sorted by (ts, sig). Returns the new ones."""
        new: list[Trade] = []
        seen = set(self._sigs)
        for t in trades:                     # dedupe against the ring AND within this batch
            if t.sig in seen:
                continue
            seen.add(t.sig)
            new.append(t)
        if not new:
            return []
        merged = sorted(list(self._trades) + new, key=lambda t: (t.ts, t.sig))
        self._trades = deque(merged[-self.ring_size:], maxlen=self.ring_size)
        self._sigs = {t.sig for t in self._trades}
        return new

    def trades(self, since_ts: int | None = None, last_n: int | None = None) -> list[Trade]:
        out = list(self._trades)
        if since_ts is not None:
            out = [t for t in out if t.ts >= since_ts]
        if last_n is not None:
            out = out[-last_n:]
        return out

    # ---- quick stats (Stage 2 features proper arrive in T6) ---------------------
    def sum_usd(self, since_ts: int, side: str | None = None) -> float:
        return sum((t.usd or 0.0) for t in self._trades if t.ts >= since_ts and (side is None or t.side == side))

    def count(self, since_ts: int, side: str | None = None) -> int:
        return sum(1 for t in self._trades if t.ts >= since_ts and (side is None or t.side == side))

    def unique_wallets(self, since_ts: int, side: str | None = None) -> int:
        return len({t.wallet for t in self._trades if t.wallet and t.ts >= since_ts and (side is None or t.side == side)})


class TapeStore:
    """All token tapes + persistence."""

    _INSERT = ("INSERT OR IGNORE INTO trades(chain, address, sig, ts, side, wallet, usd, price, amount, source, ingested_ts) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?)")

    def __init__(self, conn: sqlite3.Connection, ring_size: int = 600) -> None:
        self.conn = conn
        self.ring_size = ring_size
        self.tapes: dict[tuple[str, str], TokenTape] = {}

    def get(self, chain: str, address: str, load_from_db: bool = True) -> TokenTape:
        key = (chain, address)
        tape = self.tapes.get(key)
        if tape is None:
            tape = TokenTape(chain, address, self.ring_size)
            self.tapes[key] = tape
            if load_from_db:
                tape.add(self.load(chain, address, self.ring_size))
        return tape

    def drop(self, chain: str, address: str) -> None:
        self.tapes.pop((chain, address), None)

    def load(self, chain: str, address: str, limit: int) -> list[Trade]:
        rows = self.conn.execute(
            "SELECT sig, ts, side, wallet, usd, price, amount, source FROM trades WHERE chain=? AND address=? "
            "ORDER BY ts DESC LIMIT ?", (chain, address, limit)).fetchall()
        return [Trade(chain=chain, address=address, sig=r["sig"], ts=int(r["ts"]), side=r["side"], wallet=r["wallet"],
                      usd=r["usd"], price=r["price"], amount=r["amount"], source=r["source"],
                      tx_hash=str(r["sig"]).split(":")[0]) for r in rows]

    def ingest(self, chain: str, address: str, items: list[dict[str, Any]]) -> tuple[int, int]:
        """Normalise + dedupe + persist. Returns (new, skipped_as_duplicate_or_invalid)."""
        tape = self.get(chain, address)
        trades = [t for t in (normalize(it, chain, address) for it in items) if t is not None]
        invalid = len(items) - len(trades)
        new = tape.add(trades)
        if new:
            ingested = int(time.time())
            self.conn.execute("BEGIN")
            try:
                self.conn.executemany(self._INSERT, [
                    (t.chain, t.address, t.sig, t.ts, t.side, t.wallet, t.usd, t.price, t.amount, t.source, ingested) for t in new])
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return len(new), (len(trades) - len(new)) + invalid


@dataclass
class FetchResult:
    chain: str
    address: str
    pages: int = 0
    fetched: int = 0
    new: int = 0
    dup: int = 0
    cu: int = 0
    stopped: str = ""       # has_next=false | reached_known | page_cap | error | budget
    error: str | None = None
    next_offset: int = 0


async def fetch_tape(client: BirdeyeClient, store: TapeStore, ch: ChainConfig, address: str,
                     max_pages: int = 1, page_size: int = 100, start_offset: int = 0) -> FetchResult:
    """Pull newest trades, page by page, until: no more pages, a page contained nothing new
    (we've reached what we already know), or max_pages. Incremental by construction.
    start_offset lets a caller continue deeper into history (first-contact depth)."""
    res = FetchResult(chain=ch.name, address=address)
    tape = store.get(ch.name, address)
    cu_page, _ = cu_cost("txs_token_v3")
    offset = start_offset
    for _ in range(max_pages):
        try:
            items, has_next = await client.txs_token_v3(ch.birdeye_chain, address, limit=page_size, offset=offset)
        except BirdeyeError as e:
            res.error = str(e)
            res.stopped = "error"
            break
        res.pages += 1
        res.cu += cu_page
        res.fetched += len(items)
        new, dup = store.ingest(ch.name, address, items)
        res.new += new
        res.dup += dup
        if not items or not has_next:
            res.stopped = "has_next=false"
            break
        if new == 0 and len(tape) > 0 and start_offset == 0:
            res.stopped = "reached_known"
            break
        offset += page_size
    else:
        res.stopped = "page_cap"
    res.next_offset = offset
    tape.polls += 1
    tape.last_fetch_ts = int(time.time())
    return res


ActiveFn = Callable[[], Awaitable[list[tuple[ChainConfig, str]]]]


def persist_features(conn: sqlite3.Connection, chain: str, address: str, feats: Any, eval_ts: int,
                     wash: Any | None = None) -> int:
    """Store a TapeFeatures snapshot (+ optional WashReport) for later scoring/tuning/replay."""
    import json as _json
    d = feats.to_dict()
    wash_score = hard = soft = wash_json = None
    if wash is not None:
        wash_score = wash.wash_score
        hard = ",".join(wash.hard_vetoes) or None
        soft = ",".join(wash.soft_flags) or None
        wash_json = _json.dumps(wash.to_dict(), separators=(",", ":"), default=str)
    cur = conn.execute(
        "INSERT INTO tape_features(chain, address, as_of, eval_ts, n_trades, ofi30, ofi_recent, buyers30, sellers30, "
        "new_wallet_share, anchor_ts, since_anchor_s, price_vs_avwap_pct, price_vs_anchor_pct, features_json, "
        "wash_score, hard_vetoes, soft_flags, wash_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (chain, address, int(feats.as_of or eval_ts), eval_ts, feats.n, feats.ofi30.ofi, feats.recent.ofi,
         feats.ofi30.buyers, feats.ofi30.sellers, feats.ofi30.new_wallet_share_usd, feats.anchor_ts,
         feats.seconds_since_anchor, feats.price_vs_avwap_pct, feats.price_vs_anchor_pct,
         _json.dumps(d, separators=(",", ":"), default=str), wash_score, hard, soft, wash_json))
    return int(cur.lastrowid)


@dataclass
class PollStats:
    polled: int = 0
    fetched: int = 0
    new: int = 0
    cu: int = 0
    skipped_budget: int = 0
    skipped_refresh: int = 0
    errors: int = 0
    active: int = 0


class TapePoller:
    def __init__(self, client: BirdeyeClient, store: TapeStore, ledger: CULedger | None, settings: dict[str, Any],
                 daily_cu_cap: int, clock: Callable[[], float] = time.time) -> None:
        self.client = client
        self.store = store
        self.ledger = ledger
        self.daily_cu_cap = daily_cu_cap
        self._clock = clock
        self.interval_s = int(settings.get("poll_interval_s", 60))
        self.first_pages = int(settings.get("pages_on_first_fetch", 2))
        self.refresh_every = max(1, int(settings.get("refresh_every_n_polls", 3)))
        self.daily_cu_budget = int(settings.get("daily_cu_budget", 80_000))
        # first-contact depth: keep paging on first contact until the tape spans this much history
        # (the anchor needs a trailing baseline; hot tokens fit 200 trades in 2 minutes)
        self.first_min_span_s = int(settings.get("first_contact_min_span_s", 600))
        self.first_max_pages = int(settings.get("first_contact_max_pages", 6))
        self.deep_fetches = 0
        self.cu_today = 0
        self._day = self._day_of(clock())

    @staticmethod
    def _day_of(ts: float) -> int:
        return int(ts // 86400)

    def _roll_day(self) -> None:
        d = self._day_of(self._clock())
        if d != self._day:
            self._day = d
            self.cu_today = 0

    def _budget_ok(self, cu: int) -> bool:
        self._roll_day()
        if self.cu_today + cu > self.daily_cu_budget:
            return False
        if self.ledger is not None and self.ledger.today_total() + cu > self.daily_cu_cap:
            return False
        return True

    async def poll(self, active: list[tuple[ChainConfig, str]]) -> PollStats:
        st = PollStats(active=len(active))
        cu_page, _ = cu_cost("txs_token_v3")
        for ch, address in active:
            tape = self.store.get(ch.name, address)
            first = tape.polls == 0
            if not first and (tape.polls % self.refresh_every) != 0:
                tape.polls += 1          # count the skipped slot so the cadence holds
                st.skipped_refresh += 1
                continue
            pages = self.first_pages if first else 1
            if not self._budget_ok(cu_page * pages):
                st.skipped_budget += 1
                continue
            res = await fetch_tape(self.client, self.store, ch, address, max_pages=pages)
            st.polled += 1
            st.fetched += res.fetched
            st.new += res.new
            st.cu += res.cu
            self.cu_today += res.cu
            if res.error:
                st.errors += 1
                continue
            # first-contact depth: a hot token's 2 pages may span only a minute or two -> no baseline
            if first and res.stopped == "page_cap":
                pages_done = res.pages
                offset = res.next_offset
                while (pages_done < self.first_max_pages and tape.oldest_ts is not None and tape.newest_ts is not None
                       and tape.newest_ts - tape.oldest_ts < self.first_min_span_s and self._budget_ok(cu_page)):
                    more = await fetch_tape(self.client, self.store, ch, address, max_pages=1, start_offset=offset)
                    tape.polls -= 1                      # fetch_tape counts a poll; this is the same first contact
                    pages_done += more.pages
                    offset = more.next_offset
                    st.fetched += more.fetched
                    st.new += more.new
                    st.cu += more.cu
                    self.cu_today += more.cu
                    self.deep_fetches += 1
                    if more.error or more.stopped != "page_cap":
                        break
        return st
