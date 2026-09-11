"""T15b: active window."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scanner import clock
from scanner.budget import build, format_report
from scanner.db import open_db
from scanner.ledger import CallRecord, CULedger
from scanner.schedule import Schedule

SOFIA = ZoneInfo("Europe/Sofia")


def sofia(y, m, d, hh, mm=0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=SOFIA).timestamp())


@pytest.fixture(autouse=True)
def _tz():
    clock.configure("Europe/Sofia")
    yield
    clock.configure("UTC")


CFG = {"enabled": True, "quiet_local": {"start": "09:00", "end": "15:00"}, "mode": "off", "slow_interval_s": 300}


def test_quiet_window_boundaries_follow_configured_timezone():
    s = Schedule(CFG)
    assert not s.in_quiet(sofia(2026, 9, 11, 8, 59))
    assert s.in_quiet(sofia(2026, 9, 11, 9, 0))
    assert s.in_quiet(sofia(2026, 9, 11, 14, 59))
    assert not s.in_quiet(sofia(2026, 9, 11, 15, 0))
    assert s.state(sofia(2026, 9, 11, 10)) == "quiet(off)" and s.state(sofia(2026, 9, 11, 16)) == "active"
    # same instants read under UTC (06:00-12:00 UTC) -> the window moves with the tz, as it must
    clock.configure("UTC")
    assert not s.in_quiet(sofia(2026, 9, 11, 9, 0))        # 06:00 UTC: not quiet under UTC
    assert s.in_quiet(sofia(2026, 9, 11, 15, 30))          # 12:30 UTC: quiet under UTC


def test_scan_interval_off_slow_and_disabled():
    off = Schedule(CFG)
    assert off.scan_interval(sofia(2026, 9, 11, 10), 60) is None
    assert off.scan_interval(sofia(2026, 9, 11, 16), 60) == 60.0
    slow = Schedule({**CFG, "mode": "slow"})
    assert slow.scan_interval(sofia(2026, 9, 11, 10), 60) == 300.0
    assert slow.scan_interval(sofia(2026, 9, 11, 10), 600) == 600.0     # never faster than the base interval
    always = Schedule({**CFG, "enabled": False})
    assert always.scan_interval(sofia(2026, 9, 11, 10), 60) == 60.0 and always.state(0) == "always-on"
    assert Schedule(None).enabled is False                              # default: scan 24h
    with pytest.raises(ValueError):
        Schedule({**CFG, "mode": "pause"})
    with pytest.raises(ValueError):
        Schedule({**CFG, "quiet_local": {"start": "9am", "end": "15:00"}})


def test_next_transition_and_overnight_wrap():
    s = Schedule(CFG)
    assert s.next_transition(sofia(2026, 9, 11, 8, 30)) == sofia(2026, 9, 11, 9, 0)
    assert s.next_transition(sofia(2026, 9, 11, 10, 0)) == sofia(2026, 9, 11, 15, 0)
    assert s.next_transition(sofia(2026, 9, 11, 20, 0)) == sofia(2026, 9, 12, 9, 0)
    night = Schedule({**CFG, "quiet_local": {"start": "22:00", "end": "06:00"}})
    assert night.in_quiet(sofia(2026, 9, 11, 23)) and night.in_quiet(sofia(2026, 9, 12, 5, 59))
    assert not night.in_quiet(sofia(2026, 9, 12, 6)) and not night.in_quiet(sofia(2026, 9, 11, 12))
    assert night.next_transition(sofia(2026, 9, 11, 23)) == sofia(2026, 9, 12, 6)
    assert night.quiet_hours() == {22, 23, 0, 1, 2, 3, 4, 5}
    assert s.quiet_hours() == {9, 10, 11, 12, 13, 14}
    assert abs(s.active_fraction() - 0.75) < 1e-9 and abs(night.active_fraction() - (2 / 3)) < 1e-9
    assert "quiet 09:00-15:00 Europe/Sofia mode=off" == s.describe()


def test_budget_report_shows_quiet_block_saving(tmp_path: Path):
    conn = open_db(tmp_path / "q.sqlite")
    led = CULedger(conn)
    day = sofia(2026, 9, 10, 0)
    for h in range(24):
        led.record(CallRecord("token_list_v3", "solana", 10_000, True, 200, 5, ts=day + h * 3600 + 60))
    rep = build(conn, daily_cap=240_000, days=2, now=sofia(2026, 9, 11, 0, 30), quiet_hours=Schedule(CFG).quiet_hours())
    d = rep.days[0]
    assert d.cu == 240_000 and d.quiet_cu == 60_000 and d.projected_24h == 240_000
    assert d.projected_with_quiet == 180_000
    text = format_report(rep)
    assert "| quiet-block CU | projected with quiet |" in text and "| 60,000 | 180,000 |" in text
    assert "quiet block saves 60,000 CU/day" in text
