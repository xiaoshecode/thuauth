from __future__ import annotations

import asyncio
import math

import pytest

from thuauth.limiter import ByteLimiter


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


class ManualClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.waiters: dict[asyncio.Future[None], float] = {}

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self.waiters[future] = self.now + delay
        try:
            await future
        finally:
            del self.waiters[future]

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        for future, deadline in list(self.waiters.items()):
            if not future.done() and deadline <= self.now + 1e-9:
                future.set_result(None)
        await settle()


@pytest.mark.asyncio
async def test_fifo_does_not_let_a_small_new_request_jump_the_queue() -> None:
    clock = ManualClock()
    limiter = ByteLimiter(10, 10, clock=clock, sleep=clock.sleep)
    await limiter.acquire(10)
    order = []

    async def take(label: str, amount: int) -> None:
        await limiter.acquire(amount)
        order.append(label)

    tasks = [
        asyncio.create_task(take(label, amount))
        for label, amount in (("a", 10), ("b", 10), ("c", 1))
    ]
    await settle()
    await clock.advance(0.9)
    assert order == []
    await clock.advance(0.1)
    assert order == ["a"]
    await clock.advance(1)
    assert order == ["a", "b"]
    await clock.advance(0.11)
    await asyncio.gather(*tasks)
    assert order == ["a", "b", "c"]
    assert not clock.waiters


@pytest.mark.asyncio
async def test_cancel_head_and_middle_refund_wakes_remaining_waiter() -> None:
    clock = ManualClock()
    limiter = ByteLimiter(10, 10, clock=clock, sleep=clock.sleep)
    generation = await limiter.acquire(10)
    tasks = [asyncio.create_task(limiter.acquire(10)) for _ in range(3)]
    await settle()
    for task in tasks[:2]:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    limiter.refund(10, generation=generation)
    assert await asyncio.wait_for(tasks[2], 1) == generation
    assert not clock.waiters
    assert not limiter._queue


@pytest.mark.asyncio
async def test_refund_only_unused_bytes_and_reset_invalidates_old_credit() -> None:
    clock = ManualClock()
    limiter = ByteLimiter(10, 10, clock=clock, sleep=clock.sleep)
    generation = await limiter.acquire(10)
    limiter.refund(7, generation=generation)
    assert await limiter.acquire(7) == generation
    limiter.reset()
    limiter.refund(10, generation=generation)
    pending = asyncio.create_task(limiter.acquire(1))
    await settle()
    assert not pending.done()
    await clock.advance(0.11)
    assert await pending != generation


@pytest.mark.asyncio
async def test_resume_has_only_one_burst_not_hours_of_backlog() -> None:
    clock = ManualClock()
    limiter = ByteLimiter(10, 10, clock=clock, sleep=clock.sleep)
    await limiter.acquire(10)
    await clock.advance(86400)
    await limiter.acquire(10)
    pending = asyncio.create_task(limiter.acquire(10))
    await settle()
    assert not pending.done()
    await clock.advance(1)
    await pending


@pytest.mark.asyncio
async def test_concurrent_long_term_budget_and_fairness_are_global() -> None:
    clock = ManualClock()
    limiter = ByteLimiter(20, 8, clock=clock, sleep=clock.sleep)
    totals = [0] * 4
    observations = []

    async def worker(index: int) -> None:
        for _ in range(25):
            await limiter.acquire(4)
            totals[index] += 4
            observations.append((clock.now, sum(totals)))

    tasks = [asyncio.create_task(worker(index)) for index in range(4)]
    await settle()
    for _ in range(230):
        if all(task.done() for task in tasks):
            break
        await clock.advance(0.1)
        # Initial credit can let the first worker start two chunks ahead.
        assert max(totals) - min(totals) <= 12
    await asyncio.wait_for(asyncio.gather(*tasks), 1)
    assert totals == [100] * 4
    assert all(total <= 8 + 20 * when + 1e-6 for when, total in observations)
    assert clock.now >= (400 - 8) / 20
    assert not clock.waiters


@pytest.mark.asyncio
async def test_sleep_programming_error_is_not_swallowed() -> None:
    async def broken_sleep(delay: float) -> None:
        raise RuntimeError("synthetic clock failure")

    limiter = ByteLimiter(1, 1, sleep=broken_sleep)
    await limiter.acquire(1)
    with pytest.raises(RuntimeError, match="synthetic clock failure"):
        await limiter.acquire(1)
    assert not limiter._queue


@pytest.mark.parametrize("rate", [0, -1, math.inf, math.nan])
def test_invalid_rate(rate: float) -> None:
    with pytest.raises(ValueError):
        ByteLimiter(rate, 1)


@pytest.mark.parametrize("amount", [0, -1, 11, True, 1.5])
@pytest.mark.asyncio
async def test_invalid_reservation(amount) -> None:
    with pytest.raises(ValueError):
        await ByteLimiter(10, 10).acquire(amount)
