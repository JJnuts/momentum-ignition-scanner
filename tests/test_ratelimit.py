import asyncio

import pytest

from scanner.ratelimit import TokenBucket


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s


@pytest.mark.asyncio
async def test_burst_then_paced_at_1rps():
    clk = FakeClock()
    tb = TokenBucket(1.0, clock=clk, sleep=clk.sleep)
    t0 = clk.t
    for _ in range(4):
        await tb.acquire()
    # first call is free (bucket starts full), next three cost 1s each
    assert clk.t - t0 == pytest.approx(3.0)
    assert tb.waits == 3


@pytest.mark.asyncio
async def test_refill_after_idle_allows_capacity_burst():
    clk = FakeClock()
    tb = TokenBucket(15.0, clock=clk, sleep=clk.sleep)  # starter: 15 rps, capacity 15
    for _ in range(15):
        await tb.acquire()
    assert tb.waits == 0
    await tb.acquire()  # 16th must wait ~1/15 s
    assert tb.waits == 1
    assert tb.total_wait_s == pytest.approx(1 / 15, rel=1e-6)
    clk.t += 10  # idle -> full bucket again
    for _ in range(15):
        await tb.acquire()
    assert tb.waits == 1


@pytest.mark.asyncio
async def test_concurrent_acquirers_are_serialised():
    clk = FakeClock()
    tb = TokenBucket(1.0, clock=clk, sleep=clk.sleep)
    order: list[int] = []

    async def worker(i: int) -> None:
        await tb.acquire()
        order.append(i)

    await asyncio.gather(*(worker(i) for i in range(5)))
    assert sorted(order) == list(range(5))
    assert tb.waits == 4


def test_invalid_rate():
    with pytest.raises(ValueError):
        TokenBucket(0)
