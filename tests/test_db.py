from pathlib import Path

from scanner.db import EXPECTED_TABLES, SCHEMA_VERSION, init_db, connect, open_db, schema_version, table_names


def test_init_creates_all_tables(tmp_path: Path):
    conn = open_db(tmp_path / "data" / "t.sqlite")
    assert EXPECTED_TABLES <= table_names(conn)
    assert schema_version(conn) == SCHEMA_VERSION
    conn.close()


def test_init_is_idempotent(tmp_path: Path):
    path = tmp_path / "t.sqlite"
    conn = open_db(path)
    conn.execute("INSERT INTO meta(key, value) VALUES('x', '1')")
    init_db(conn)  # second init must not wipe data
    assert conn.execute("SELECT value FROM meta WHERE key='x'").fetchone()["value"] == "1"
    conn.close()


def test_wal_mode_and_row_factory(tmp_path: Path):
    conn = connect(tmp_path / "t.sqlite")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    init_db(conn)
    row = conn.execute("SELECT key FROM meta").fetchone()
    assert row["key"] == "schema_version"
    conn.close()


def test_v1_database_is_migrated_to_current(tmp_path: Path):
    from scanner.db import SCHEMA, column_names

    path = tmp_path / "old.sqlite"
    conn = connect(path)
    conn.execute("BEGIN")
    for stmt in SCHEMA:  # pure v1 schema
        conn.execute(stmt)
    conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '1')")
    conn.execute("INSERT INTO scan_rows(chain, cycle_id, ts, address) VALUES('solana', 1, 1, 'A')")
    conn.execute("COMMIT")
    assert "vol_24h" not in column_names(conn, "scan_rows")
    init_db(conn)
    assert schema_version(conn) == SCHEMA_VERSION
    cols = column_names(conn, "scan_rows")
    for c in ("vol_24h", "uw_24h", "sort_key", "rank"):
        assert c in cols
    # existing data survives
    assert conn.execute("SELECT address FROM scan_rows").fetchone()["address"] == "A"
    init_db(conn)  # idempotent at current version
    conn.close()


def test_newer_schema_is_refused(tmp_path: Path):
    conn = open_db(tmp_path / "t.sqlite")
    conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    import pytest
    with pytest.raises(RuntimeError, match="newer"):
        init_db(conn)
    conn.close()


def test_trades_dedupe_by_signature(tmp_path: Path):
    conn = open_db(tmp_path / "t.sqlite")
    ins = "INSERT OR IGNORE INTO trades(chain,address,sig,ts,side,wallet,usd) VALUES(?,?,?,?,?,?,?)"
    conn.execute(ins, ("solana", "A", "sig1", 1, "buy", "w1", 10.0))
    conn.execute(ins, ("solana", "A", "sig1", 1, "buy", "w1", 10.0))
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    conn.close()
