from pathlib import Path

from scanner.recorder import RawRecorder


def test_record_and_read_back(tmp_path: Path):
    rec = RawRecorder(tmp_path / "raw")
    p1 = rec.record("token_list_v3", {"chain": "solana", "limit": 100}, 200, {"data": {"items": [1, 2]}},
                    chain="solana", latency_ms=123, ts=1_700_000_000)
    p2 = rec.record("ohlcv_v3", {"address": "X"}, 200, {"data": []}, chain="solana", ts=1_700_000_001)
    assert p1 == p2  # same UTC day → same file
    recs = list(rec.iter_file(p1))
    assert [r["endpoint"] for r in recs] == ["token_list_v3", "ohlcv_v3"]
    assert recs[0]["params"]["limit"] == 100
    assert recs[0]["payload"]["data"]["items"] == [1, 2]
    assert recs[0]["latency_ms"] == 123
    assert rec.records_written == 2


def test_multi_member_gzip_append_survives_reopen(tmp_path: Path):
    d = tmp_path / "raw"
    RawRecorder(d).record("a", {}, 200, 1, ts=1_700_000_000)
    RawRecorder(d).record("b", {}, 200, 2, ts=1_700_000_000)  # new instance = new gzip member
    rec = RawRecorder(d)
    assert [r["endpoint"] for r in rec.iter_day("2023-11-14")] == ["a", "b"]


def test_day_rollover(tmp_path: Path):
    rec = RawRecorder(tmp_path / "raw")
    rec.record("a", {}, 200, 1, ts=1_700_000_000)          # 2023-11-14 UTC
    rec.record("b", {}, 200, 2, ts=1_700_000_000 + 86400)  # 2023-11-15 UTC
    assert len(rec.files()) == 2
    assert list(rec.iter_day("2023-11-16")) == []


def test_disabled_recorder_writes_nothing(tmp_path: Path):
    rec = RawRecorder(tmp_path / "raw", enabled=False)
    assert rec.record("a", {}, 200, 1) is None
    assert not (tmp_path / "raw").exists()
