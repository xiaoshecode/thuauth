"""Supervise health checks, traffic and statistics, including Windows resume gaps."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Protocol

from thuauth.auth import AuthService

logger = logging.getLogger(__name__)


class Traffic(Protocol):
    async def run(self) -> None: ...
    def reset(self) -> None: ...
    def snapshot(self) -> dict[str, object]: ...


class Daemon:
    def __init__(
        self,
        service: AuthService,
        traffic: Traffic,
        reset_connections: Callable[[], Awaitable[None]],
        *,
        clock: Callable[[], float] = time.monotonic,
        monitor_interval: float = 1.0,
        stats_interval: float = 60.0,
    ) -> None:
        self.service = service
        self.traffic = traffic
        self.reset_connections = reset_connections
        self.clock = clock
        self.monitor_interval = monitor_interval
        self.stats_interval = stats_interval

    async def _health(self) -> None:
        while True:
            if self.service.config.auth.auto_relogin:
                await self.service.ensure_all()
            else:
                await self.service.status_all()
            await asyncio.sleep(self.service.config.auth.health_interval_seconds)

    async def _statistics(self) -> None:
        while True:
            await asyncio.sleep(self.stats_interval)
            logger.info("traffic_stats %s", json.dumps(self.traffic.snapshot(), ensure_ascii=True))

    async def _monitor(self, stop: asyncio.Event) -> bool:
        last = self.clock()
        gap_limit = max(60.0, 3 * self.service.config.auth.health_interval_seconds)
        while not stop.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.monitor_interval)
            now = self.clock()
            if not stop.is_set() and (now - last > gap_limit or now < last):
                self.service.invalidate()
                return True
            last = now
        return False

    async def _require_running(
        self, name: str, operation: Awaitable[None], stopping: asyncio.Event
    ) -> None:
        try:
            await operation
        except asyncio.CancelledError:
            if not stopping.is_set():
                raise RuntimeError(f"daemon worker cancelled unexpectedly: {name}") from None
            raise
        raise RuntimeError(f"daemon worker exited unexpectedly: {name}")

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            restart = False
            stopping = asyncio.Event()
            async with asyncio.TaskGroup() as group:
                tasks = [
                    group.create_task(self._require_running("health", self._health(), stopping)),
                    group.create_task(
                        self._require_running("traffic", self.traffic.run(), stopping)
                    ),
                    group.create_task(
                        self._require_running("statistics", self._statistics(), stopping)
                    ),
                ]
                try:
                    restart = await self._monitor(stop)
                finally:
                    # TaskGroup does not cancel infinite children on normal scope exit.
                    stopping.set()
                    for task in tasks:
                        task.cancel()
            if restart:
                await self.reset_connections()
                self.traffic.reset()
                logger.info("network_resume sessions_reset=true")
