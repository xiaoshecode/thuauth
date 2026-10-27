"""A FIFO, event-loop-local byte bucket with a bounded idle burst."""

from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import Awaitable, Callable
from time import monotonic

type Clock = Callable[[], float]
type Sleep = Callable[[float], Awaitable[None]]


class ByteLimiter:
    """Reserve before reading and refund unused bytes in a finally block.

    Mutations are synchronous on one event loop; no lock is held while waiting.
    Only the FIFO head runs a timer. A reservation's generation prevents an old
    read from restoring discarded tokens after reset().
    """

    def __init__(
        self,
        rate_bytes_per_second: float,
        burst_bytes: int,
        *,
        clock: Clock = monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not math.isfinite(rate_bytes_per_second) or rate_bytes_per_second <= 0:
            raise ValueError("byte rate must be finite and positive")
        if isinstance(burst_bytes, bool) or not isinstance(burst_bytes, int) or burst_bytes < 1:
            raise ValueError("burst must be a positive integer")
        self.rate = rate_bytes_per_second
        self.burst_bytes = burst_bytes
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(burst_bytes)
        self._updated = clock()
        self._generation = 0
        self._queue: deque[asyncio.Event] = deque()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(
            self.burst_bytes, self._tokens + max(0.0, now - self._updated) * self.rate
        )
        self._updated = now

    def _wake(self) -> None:
        if self._queue:
            self._queue[0].set()

    async def _wait(self, event: asyncio.Event, delay: float) -> None:
        timer = asyncio.ensure_future(self._sleep(delay))
        changed = asyncio.create_task(event.wait())
        try:
            done, _ = await asyncio.wait((timer, changed), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            timer.cancel()
            changed.cancel()
            await asyncio.gather(timer, changed, return_exceptions=True)

    async def acquire(self, amount: int) -> int:
        """Debit amount bytes fairly; return the generation for refund()."""
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise ValueError("reservation must be an integer")
        if not 1 <= amount <= self.burst_bytes:
            raise ValueError("reservation must fit within the burst")
        event = asyncio.Event()
        self._queue.append(event)
        try:
            while True:
                event.clear()
                if self._queue[0] is not event:
                    await event.wait()
                    continue
                self._refill()
                if self._tokens >= amount:
                    self._tokens -= amount
                    return self._generation
                await self._wait(event, (amount - self._tokens) / self.rate)
        finally:
            self._queue.remove(event)
            self._wake()

    def refund(self, amount: int, *, generation: int) -> None:
        """Return only unread bytes, once, from a completed reservation."""
        if not 0 <= amount <= self.burst_bytes:
            raise ValueError("refund must fit within the burst")
        if generation == self._generation:
            self._refill()
            self._tokens = min(self.burst_bytes, self._tokens + amount)
            self._wake()

    def reset(self) -> None:
        """Discard idle credit, including refunds from pre-reset reservations."""
        self._generation += 1
        self._tokens = 0.0
        self._updated = self._clock()
        self._wake()
