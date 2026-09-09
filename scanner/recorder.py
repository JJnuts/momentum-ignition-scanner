"""Raw API response recorder.

Every API response is appended to raw/YYYY-MM-DD.jsonl.gz as one JSON line:
  {"ts", "endpoint", "chain", "params", "status", "latency_ms", "payload"}

Why: every later task can be developed and tested offline against recorded
responses (no CU spend), and the T15 replay backtester reads these files.
Never disable in production.

gzip append mode writes a new gzip member per record; gzip.open("rt") reads
multi-member files transparently.
"""
from __future__ import annotations

import gzip
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class RawRecorder:
    def __init__(self, directory: Path, enabled: bool = True) -> None:
        self.directory = Path(directory)
        self.enabled = enabled
        self.records_written = 0
        if self.enabled:
            self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _day(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    def path_for(self, ts: float) -> Path:
        return self.directory / f"{self._day(ts)}.jsonl.gz"

    def record(
        self,
        endpoint: str,
        params: dict[str, Any] | None,
        status: int | None,
        payload: Any,
        chain: str | None = None,
        latency_ms: int | None = None,
        ts: float | None = None,
    ) -> Path | None:
        if not self.enabled:
            return None
        ts = time.time() if ts is None else ts
        rec = {
            "ts": round(ts, 3),
            "endpoint": endpoint,
            "chain": chain,
            "params": params or {},
            "status": status,
            "latency_ms": latency_ms,
            "payload": payload,
        }
        line = json.dumps(rec, separators=(",", ":"), ensure_ascii=False, default=str) + "\n"
        path = self.path_for(ts)
        with gzip.open(path, "at", encoding="utf-8") as fh:
            fh.write(line)
        self.records_written += 1
        return path

    def iter_file(self, path: Path) -> Iterator[dict[str, Any]]:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def iter_day(self, day: str) -> Iterator[dict[str, Any]]:
        """day = 'YYYY-MM-DD' (UTC)."""
        path = self.directory / f"{day}.jsonl.gz"
        if not path.exists():
            return iter(())
        return self.iter_file(path)

    def files(self) -> list[Path]:
        return sorted(self.directory.glob("*.jsonl.gz"))
