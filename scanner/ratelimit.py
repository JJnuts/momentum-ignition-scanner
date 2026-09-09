"""Async token-bucket rate limiter with injectable clock/sleep (for tests)."""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable


class TokenBucket:
    def __init__(
        self,
        rate_per_s: float,
        capacity: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_s <= 0:
            raise ValueError("rate_per_s must be > 0")
        self.rate = float(rate_per_s)
        self.capacity = float(capacity) if capacity is not None else max(1.0, self.rate)
        self.tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock: asyncio.Lock | None = None
        self.waits = 0
        self.total_wait_s = 0.0

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

    async def acquire(self, n: float = 1.0) -> None:
        if self._lock is None:  # created lazily inside a running loop
            self._lock = asyncio.Lock()
        async with self._lock:  # FIFO: holders wait in order
            while True:
                self._refill()
                if self.tokens >= n:
                    self.tokens -= n
                    return
                wait = (n - self.tokens) / self.rate
                self.waits += 1
                self.total_wait_s += wait
                await self._sleep(wait)
