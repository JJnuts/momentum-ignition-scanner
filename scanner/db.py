"""SQLite schema, migrations and connection helpers.

Rules:
  - SCHEMA holds the v1 CREATE statements and is FROZEN. Never edit it.
  - Every later change is an entry in MIGRATIONS[version] (ALTER/CREATE only).
  - init_db() creates the base schema, then applies migrations up to
    SCHEMA_VERSION, so a fresh DB and an old DB end up identical.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 6

SCHEMA: list[str] = [
    # key/value state (schema version, cursors, daily budget counters)
    """CREATE TABLE IF NOT EXISTS meta(
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    # Stage-0 wide scan snapshots (one row per token per cycle)
    """CREATE TABLE IF NOT EXISTS scan_rows(
        id            INTEGER PRIMARY KEY,
        chain         TEXT    NOT NULL,
        cycle_id      INTEGER NOT NULL,
        ts            INTEGER NOT NULL,
        address       TEXT    NOT NULL,
        symbol        TEXT,
        name          TEXT,
        price         REAL,
        liquidity     REAL,
        market_cap    REAL,
        fdv           REAL,
        holder        INTEGER,
        listing_ts    INTEGER,
        last_trade_ts INTEGER,
        vol_1m        REAL, vol_5m REAL, vol_30m REAL, vol_1h REAL,
        vol_1m_chg    REAL, vol_5m_chg REAL,
        pc_1m         REAL, pc_5m REAL, pc_30m REAL, pc_1h REAL,
        tr_1m         INTEGER, tr_5m INTEGER, tr_30m INTEGER, tr_1h INTEGER,
        raw_json      TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_scan_rows_chain_addr_ts ON scan_rows(chain, address, ts)",
    "CREATE INDEX IF NOT EXISTS ix_scan_rows_ts ON scan_rows(ts)",
    # Stage-1 nominations (every Stage-1 pass, incl. WATCH) and later tier decisions
    """CREATE TABLE IF NOT EXISTS nominations(
        id            INTEGER PRIMARY KEY,
        chain         TEXT    NOT NULL,
        address       TEXT    NOT NULL,
        ts            INTEGER NOT NULL,
        tier          TEXT    NOT NULL,      -- WATCH | IGNITION | CONFIRMED | VETO
        score         REAL,
        price         REAL,
        liquidity     REAL,
        features_json TEXT    NOT NULL,
        gates_json    TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_nominations_chain_addr_ts ON nominations(chain, address, ts)",
    # Active candidate set (Stage-2 deep polling)
    """CREATE TABLE IF NOT EXISTS candidates(
        chain         TEXT NOT NULL,
        address       TEXT NOT NULL,
        first_seen_ts INTEGER NOT NULL,
        last_seen_ts  INTEGER NOT NULL,
        anchor_ts     INTEGER,
        anchor_price  REAL,
        status        TEXT NOT NULL DEFAULT 'active',
        state_json    TEXT,
        PRIMARY KEY(chain, address)
    )""",
    # Trade tape (deduped by signature)
    """CREATE TABLE IF NOT EXISTS trades(
        chain    TEXT    NOT NULL,
        address  TEXT    NOT NULL,
        sig      TEXT    NOT NULL,
        ts       INTEGER NOT NULL,
        side     TEXT    NOT NULL,           -- buy | sell
        wallet   TEXT,
        usd      REAL,
        price    REAL,
        amount   REAL,
        source   TEXT,
        PRIMARY KEY(chain, address, sig)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_trades_chain_addr_ts ON trades(chain, address, ts)",
    # Alerts actually sent
    """CREATE TABLE IF NOT EXISTS alerts(
        id            INTEGER PRIMARY KEY,
        nomination_id INTEGER,
        chain         TEXT    NOT NULL,
        address       TEXT    NOT NULL,
        ts            INTEGER NOT NULL,
        tier          TEXT    NOT NULL,
        score         REAL,
        price         REAL,
        liquidity     REAL,
        card_json     TEXT,
        channel       TEXT,
        message_id    TEXT,
        status        TEXT    NOT NULL DEFAULT 'sent'
    )""",
    "CREATE INDEX IF NOT EXISTS ix_alerts_chain_addr_ts ON alerts(chain, address, ts)",
    # Labeler: forward snapshots for nominations, alerts, and control samples
    """CREATE TABLE IF NOT EXISTS labels(
        id          INTEGER PRIMARY KEY,
        ref_kind    TEXT    NOT NULL,        -- nomination | alert | control
        ref_id      INTEGER,
        chain       TEXT    NOT NULL,
        address     TEXT    NOT NULL,
        t0_ts       INTEGER NOT NULL,
        t0_price    REAL,
        t0_liq      REAL,
        horizon_min INTEGER NOT NULL,        -- 5 | 15 | 30 | 60
        due_ts      INTEGER NOT NULL,
        done_ts     INTEGER,
        price       REAL,
        high        REAL,
        low         REAL,
        liquidity   REAL,
        status      TEXT    NOT NULL DEFAULT 'pending'   -- pending | done | failed
    )""",
    "CREATE INDEX IF NOT EXISTS ix_labels_due ON labels(status, due_ts)",
    "CREATE INDEX IF NOT EXISTS ix_labels_ref ON labels(ref_kind, ref_id)",
    # Safety verdicts (cached)
    """CREATE TABLE IF NOT EXISTS safety(
        chain       TEXT NOT NULL,
        address     TEXT NOT NULL,
        checked_ts  INTEGER NOT NULL,
        verdict     TEXT NOT NULL,           -- SAFE | UNSAFE | UNKNOWN
        result_json TEXT,
        PRIMARY KEY(chain, address)
    )""",
    # Compute-unit ledger (every API call)
    """CREATE TABLE IF NOT EXISTS cu_ledger(
        id         INTEGER PRIMARY KEY,
        ts         INTEGER NOT NULL,
        chain      TEXT,
        endpoint   TEXT    NOT NULL,
        cu         INTEGER NOT NULL,
        verified   INTEGER NOT NULL DEFAULT 0,
        status     INTEGER,
        latency_ms INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS ix_cu_ledger_ts ON cu_ledger(ts)",
]

# version -> statements that take a DB from version-1 to version.
MIGRATIONS: dict[int, list[str]] = {
    2: [  # T2: extra list-v3 fields (24h windows exist on every chain; RH lacks 1m/5m/30m)
        "ALTER TABLE scan_rows ADD COLUMN vol_24h REAL",
        "ALTER TABLE scan_rows ADD COLUMN vol_1h_chg REAL",
        "ALTER TABLE scan_rows ADD COLUMN pc_24h REAL",
        "ALTER TABLE scan_rows ADD COLUMN tr_24h INTEGER",
        "ALTER TABLE scan_rows ADD COLUMN uw_24h INTEGER",
        "ALTER TABLE scan_rows ADD COLUMN buy_24h INTEGER",
        "ALTER TABLE scan_rows ADD COLUMN sell_24h INTEGER",
        "ALTER TABLE scan_rows ADD COLUMN sort_key TEXT",
        "ALTER TABLE scan_rows ADD COLUMN rank INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_scan_rows_chain_cycle ON scan_rows(chain, cycle_id)",
    ],
    3: [  # T4: labeler bookkeeping
        "ALTER TABLE labels ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE labels ADD COLUMN path_status TEXT NOT NULL DEFAULT 'pending'",  # pending|done|failed|skipped
        "ALTER TABLE labels ADD COLUMN source TEXT",                                   # scan_row|multi_price|ohlcv
        "CREATE INDEX IF NOT EXISTS ix_labels_path ON labels(path_status, due_ts)",
    ],
    4: [  # T6: per-poll Stage-2 feature snapshots (inputs for scoring, tune.py and the replay backtester)
        """CREATE TABLE IF NOT EXISTS tape_features(
            id            INTEGER PRIMARY KEY,
            chain         TEXT    NOT NULL,
            address       TEXT    NOT NULL,
            as_of         INTEGER NOT NULL,      -- tape's newest trade ts at evaluation
            eval_ts       INTEGER NOT NULL,      -- wall clock of the evaluation
            n_trades      INTEGER,
            ofi30         REAL,
            ofi_recent    REAL,
            buyers30      INTEGER,
            sellers30     INTEGER,
            new_wallet_share REAL,
            anchor_ts     INTEGER,
            since_anchor_s INTEGER,
            price_vs_avwap_pct REAL,
            price_vs_anchor_pct REAL,
            features_json TEXT    NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS ix_tape_features_chain_addr_ts ON tape_features(chain, address, as_of)",
    ],
    5: [  # T7: wash score + vetoes per snapshot; enrichment cache
        "ALTER TABLE tape_features ADD COLUMN wash_score REAL",
        "ALTER TABLE tape_features ADD COLUMN hard_vetoes TEXT",     # comma-separated names
        "ALTER TABLE tape_features ADD COLUMN soft_flags TEXT",
        "ALTER TABLE tape_features ADD COLUMN wash_json TEXT",
        """CREATE TABLE IF NOT EXISTS enrichment(
            chain      TEXT NOT NULL,
            address    TEXT NOT NULL,
            kind       TEXT NOT NULL,          -- holdings | tag_flows
            fetched_ts INTEGER NOT NULL,
            payload    TEXT NOT NULL,
            PRIMARY KEY(chain, address, kind)
        )""",
    ],
    6: [  # T9: scored decisions (one per candidate per poll); T13 reads alertable rows
        """CREATE TABLE IF NOT EXISTS decisions(
            id               INTEGER PRIMARY KEY,
            chain            TEXT    NOT NULL,
            address          TEXT    NOT NULL,
            eval_ts          INTEGER NOT NULL,
            as_of            INTEGER,
            anchor_ts        INTEGER,
            anchor_source    TEXT,
            since_anchor_s   INTEGER,
            score            REAL    NOT NULL,
            tier             TEXT    NOT NULL,      -- VETO | CONFIRMED | IGNITION | WATCH
            eligible         INTEGER NOT NULL,
            alertable        INTEGER NOT NULL,
            hard_vetoes      TEXT,
            soft_flags       TEXT,
            components_json  TEXT    NOT NULL,      -- per component: points, inputs, data_ts
            tape_features_id INTEGER,
            alerted_ts       INTEGER                -- set by T13 when a ping is sent
        )""",
        "CREATE INDEX IF NOT EXISTS ix_decisions_chain_addr_ts ON decisions(chain, address, eval_ts)",
        "CREATE INDEX IF NOT EXISTS ix_decisions_alertable ON decisions(alertable, alerted_ts)",
    ],
}

EXPECTED_TABLES = {
    "meta", "scan_rows", "nominations", "candidates", "trades",
    "alerts", "labels", "safety", "cu_ledger", "tape_features", "decisions",
}


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit; use BEGIN explicitly
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def schema_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return int(row["value"]) if row else None


def init_db(conn: sqlite3.Connection) -> None:
    """Create the base schema (idempotent) and apply pending migrations."""
    conn.execute("BEGIN")
    try:
        for stmt in SCHEMA:
            conn.execute(stmt)
        current = schema_version(conn) or 1
        if current > SCHEMA_VERSION:
            raise RuntimeError(f"database schema v{current} is newer than this code (v{SCHEMA_VERSION})")
        for version in range(current + 1, SCHEMA_VERSION + 1):
            for stmt in MIGRATIONS.get(version, []):
                conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r["name"] for r in rows}


def column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def meta_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


def open_db(path: Path) -> sqlite3.Connection:
    conn = connect(path)
    init_db(conn)
    return conn
