"""T15a: local-day clock + budget report."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scanner import clock, ledger as ledger_mod
from scanner.budget import build, format_report
from scanner.db import open_db as connect
from scanner.ledger import CallRecord, CULedger

SOFIA = ZoneInfo("Europe/Sofia")


def sofia(y, m, d, hh, mm=0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=SOFIA).timestamp())


@pytest.fixture(autouse=True)
def _utc_after():
    yield
    clock.configure("UTC")


def test_day_start_follows_configured_timezone():
    ts = sofia(2026, 9, 11, 1, 30)                       # 01:30 Sofia = 22:30 UTC the day before
    clock.configure("UTC")
    assert clock.local_date(ts) == "2026-09-10"
    assert clock.day_start(ts) == int(datetime(2026, 9, 10, tzinfo=ZoneInfo("UTC")).timestamp())
    clock.configure("Europe/Sofia")
    assert clock.local_date(ts) == "2026-09-11"
    assert clock.day_start(ts) == sofia(2026, 9, 11, 0)
    assert clock.next_day_start(ts) == sofia(2026, 9, 12, 0)
    assert clock.local_hour(ts) == 1
    # the day rolls at local midnight, NOT at 03:00 local (old UTC midnight)
    assert clock.day_key(sofia(2026, 9, 11, 23, 59)) != clock.day_key(sofia(2026, 9, 12, 0, 1))
    assert clock.day_key(sofia(2026, 9, 11, 2, 59)) == clock.day_key(sofia(2026, 9, 11, 3, 1))
    with pytest.raises(ValueError):
        clock.configure("Mars/Olympus")


def test_ledger_today_total_uses_local_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock.configure("Europe/Sofia")
    conn = connect(tmp_path / "t.sqlite")
    led = CULedger(conn)
    led.record(CallRecord("token_list_v3", "solana", 50, True, 200, 10, ts=sofia(2026, 9, 10, 23, 30)))   # yesterday local
    led.record(CallRecord("token_list_v3", "solana", 70, True, 200, 10, ts=sofia(2026, 9, 11, 0, 30)))    # today local
    monkeypatch.setattr(ledger_mod.time, "time", lambda: float(sofia(2026, 9, 11, 1, 0)))
    assert led.today_total() == 70          # under UTC it would have been 120 (both rows in the same UTC day)
    clock.configure("UTC")
    assert led.today_total() == 120


def test_budget_report_dark_hours_cap_hit_and_projection(tmp_path: Path):
    clock.configure("Europe/Sofia")
    conn = connect(tmp_path / "b.sqlite")
    led = CULedger(conn)
    day = sofia(2026, 9, 10, 0)
    # 16 active hours at 15k each (cap 240k crossed in hour 15), then nothing until midnight
    for h in range(16):
        for k in range(3):
            led.record(CallRecord("txs_token_v3", "solana", 5000, True, 200, 10, ts=day + h * 3600 + k * 1200))
    now = sofia(2026, 9, 11, 0, 30)
    rep = build(conn, daily_cap=240_000, days=2, now=now)
    d0, d1 = rep.days
    assert d0.date == "2026-09-10" and d0.cu == 240_000 and d0.calls == 48
    assert d0.active_hours == 16 and d0.dark_hours == 8 and d0.observed_hours == 24
    assert d0.cap_hit_ts == day + 15 * 3600 + 2 * 1200          # the 48th call
    assert d0.cu_per_active_hour == 15_000 and d0.projected_24h == 360_000
    assert d1.date == "2026-09-11" and d1.cu == 0 and d1.observed_hours == 1 and d1.dark_hours == 1
    assert rep.by_endpoint == [("txs_token_v3", 48, 240_000)]
    text = format_report(rep)
    assert "tz Europe/Sofia" in text and "| 2026-09-10 | 240,000 | 48 | 15:40 | 16/24 | 8 |" in text
    assert "360,000 | OVER cap, 8 dark h |" in text and "txs_token_v3 | 48 | 240,000 | 100% |" in text
    assert "dark hours total: 9" in text


def test_budget_report_empty_ledger(tmp_path: Path):
    conn = connect(tmp_path / "e.sqlite")
    rep = build(conn, daily_cap=1000, days=1, now=sofia(2026, 9, 11, 12))
    assert len(rep.days) == 1 and rep.days[0].cu == 0 and rep.by_endpoint == []
    assert "within cap" in format_report(rep)
