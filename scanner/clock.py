"""Local-day clock (T15a). ONE definition of "today" for every daily budget.

Before T15a each module computed `ts // 86400` (UTC midnight). With the user in
Europe/Sofia (UTC+3) the daily CU cap reset at 03:00 local, and once the cap
was hit mid-afternoon the scanner went dark through the whole US session.
Now every daily budget (ledger cap, tape / enrichment / safety / rug-watch
sub-budgets, candidate degrade) rolls at local midnight in `config.timezone`.

`configure()` is called once at start-up by the runner / CLI. Unconfigured
(tests, library use) the clock is UTC, which is the pre-T15a behaviour.
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_TZ_NAME = "UTC"
_TZ = ZoneInfo("UTC")


def configure(tz_name: str | None) -> str:
    """Set the local timezone (IANA name, e.g. 'Europe/Sofia'). Returns the name in effect."""
    global _TZ_NAME, _TZ
    name = (tz_name or "UTC").strip() or "UTC"
    try:
        _TZ = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"unknown timezone {name!r}") from e
    _TZ_NAME = name
    return _TZ_NAME


def tz_name() -> str:
    return _TZ_NAME


def day_start(ts: float) -> int:
    """Epoch seconds of local midnight on the local day containing `ts`."""
    d = datetime.fromtimestamp(ts, _TZ).date()
    return int(datetime.combine(d, dtime(0), tzinfo=_TZ).timestamp())


def next_day_start(ts: float) -> int:
    d = datetime.fromtimestamp(ts, _TZ).date() + timedelta(days=1)
    return int(datetime.combine(d, dtime(0), tzinfo=_TZ).timestamp())


def day_key(ts: float) -> int:
    """Opaque per-day key (== day_start). Modules compare it to detect a day roll and pass it to
    ledger.total_since() as the start of 'today'."""
    return day_start(ts)


def local_hour(ts: float) -> int:
    return datetime.fromtimestamp(ts, _TZ).hour


def local_minutes(ts: float) -> int:
    """Minutes since local midnight (0..1439)."""
    dt = datetime.fromtimestamp(ts, _TZ)
    return dt.hour * 60 + dt.minute


def local_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, _TZ).strftime("%Y-%m-%d")


def local_hms(ts: float) -> str:
    return datetime.fromtimestamp(ts, _TZ).strftime("%H:%M")
