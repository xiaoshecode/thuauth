"""Bounded observations and opt-in bulk traffic; never performs authentication."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC
from email.utils import parsedate_to_datetime
from ipaddress import ip_address
from time import monotonic, time
from urllib.parse import urlsplit

import aiohttp

from thuauth.config import Config
from thuauth.limiter import ByteLimiter, Clock, Sleep
from thuauth.models import IPFamily, Target


@dataclass
class _Stats:
    requests: int = 0
    bytes: int = 0
    successes: int = 0
    failures: int = 0
    aborted: int = 0
    redirects: int = 0
    last_status: int | None = None
    last_result: str = "idle"


@dataclass
class _TransportState:
    failures: int = 0
    until: float = 0.0
    version: int = 0


@dataclass
class _TargetState:
    failures: int = 0
    until: float = 0.0
    throttled_until: float = 0.0
    next_request: float = 0.0
    paused: bool = False
    version: int = 0
    transport: dict[IPFamily, _TransportState] = field(
        default_factory=lambda: {"ipv4": _TransportState(), "ipv6": _TransportState()}
    )


def _looks_like_html(prefix: bytes) -> bool:
    """Bounded sniffing, including BOM, whitespace and split leading comments."""
    beginning = prefix.removeprefix(b"\xef\xbb\xbf").lstrip().lower()
    while beginning.startswith(b"<!--"):
        end = beginning.find(b"-->", 4)
        if end < 0:
            # Reject an overlong markup prefix rather than counting it as a file.
            return len(prefix) >= 512
        beginning = beginning[end + 3 :].lstrip()
    return beginning.startswith(
        (b"<!doctype html", b"<html", b"<head", b"<body", b"<form", b"<script")
    )


class TrafficRunner:
    def __init__(
        self,
        config: Config,
        sessions: Mapping[IPFamily, aiohttp.ClientSession],
        is_online: Callable[[IPFamily], bool],
        *,
        clock: Clock = monotonic,
        sleep: Sleep = asyncio.sleep,
        wall_clock: Clock = time,
        connect_timeout: float = 5.0,
        read_timeout: float = 10.0,
        request_interval: float = 1.0,
    ) -> None:
        for value in (connect_timeout, read_timeout, request_interval):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("traffic timeouts and interval must be finite and positive")
        self._config = config
        # Keep the original mapping: SessionPool.reset() replaces its entries.
        self._sessions = sessions
        self._is_online = is_online
        self._clock = clock
        self._sleep = sleep
        self._wall_clock = wall_clock
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._request_interval = request_interval
        enabled_families: dict[IPFamily, bool] = {
            "ipv4": config.auth.ipv4,
            "ipv6": config.auth.ipv6,
        }
        self._families = tuple(
            family for family, enabled in enabled_families.items() if enabled and family in sessions
        )
        self._workers: dict[IPFamily, int] = {
            "ipv4": config.bulk.workers_ipv4,
            "ipv6": config.bulk.workers_ipv6,
        }
        rate = config.bulk.download_mib_per_minute * 1024 * 1024 / 60
        # At most one second or one small chunk of idle credit, across all workers.
        burst = min(config.bulk.chunk_bytes, 65536, max(1, math.ceil(rate)))
        self._limiter = ByteLimiter(rate, burst, clock=clock, sleep=sleep)
        self._stats = {
            mode: {family: _Stats() for family in self._families} for mode in ("probe", "bulk")
        }
        # Identical URLs on two stacks share server backoff. Keys are never exposed.
        self._states = {
            target.url: _TargetState() for target in config.probe.targets + config.bulk.targets
        }
        self._running = False

    def reset(self) -> None:
        self._limiter.reset()

    def snapshot(self) -> dict[str, object]:
        now = self._clock()
        result: dict[str, object] = {
            mode: {family: asdict(stats) for family, stats in families.items()}
            for mode, families in self._stats.items()
        }
        result["targets"] = {
            mode: [
                {
                    "index": index,
                    "failures": self._states[target.url].failures,
                    "paused": self._states[target.url].paused,
                    "cooldown_seconds": max(0.0, self._states[target.url].until - now),
                    "transport": {
                        family: {
                            "failures": local.failures,
                            "cooldown_seconds": max(0.0, local.until - now),
                        }
                        for family, local in self._states[target.url].transport.items()
                        if family in self._families
                    },
                }
                for index, target in enumerate(targets)
            ]
            for mode, targets in (
                ("probe", self._config.probe.targets),
                ("bulk", self._config.bulk.targets),
            )
        }
        result["rate_bytes_per_second"] = self._limiter.rate
        result["burst_bytes"] = self._limiter.burst_bytes
        return result

    async def _supervise(self, operation: Awaitable[None], stopping: asyncio.Event) -> None:
        try:
            await operation
        except asyncio.CancelledError:
            if not stopping.is_set():
                raise RuntimeError("traffic worker cancelled unexpectedly") from None
            raise
        raise RuntimeError("traffic worker exited unexpectedly")

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("traffic runner is already running")
        self._running = True
        stopping = asyncio.Event()
        try:
            async with asyncio.TaskGroup() as group:
                if self._config.probe.targets:
                    group.create_task(
                        self._supervise(self._probe_loop(), stopping), name="traffic-probe"
                    )
                if self._config.bulk.enabled and self._config.bulk.targets:
                    for family in self._families:
                        for worker in range(self._workers[family]):
                            group.create_task(
                                self._supervise(self._bulk_loop(family, worker), stopping),
                                name=f"traffic-{family}-{worker}",
                            )
                # Even an empty configuration is a long-lived daemon component.
                try:
                    await asyncio.Future[None]()
                finally:
                    stopping.set()
        finally:
            self._running = False

    def _cooling(self, state: _TargetState, family: IPFamily | None = None) -> bool:
        now = self._clock()
        return (
            state.paused
            or state.until > now
            or (family is not None and state.transport[family].until > now)
        )

    async def _probe_loop(self) -> None:
        targets = self._config.probe.targets
        index = 0
        while True:
            target = targets[index]
            state = self._states[target.url]
            if not self._cooling(state):
                for family in self._families:
                    # A failed stack must not suppress this tick's other observation.
                    # An explicit server throttle still applies immediately to both.
                    if state.paused or state.throttled_until > self._clock():
                        break
                    if (
                        target.family in (family, "both")
                        and state.transport[family].until <= self._clock()
                    ):
                        await self._fetch(family, target, state, bulk=False)
            index = (index + 1) % len(targets)
            # Skip missed ticks; UNKNOWN retains this observation path.
            await self._sleep(self._config.probe.interval_seconds)

    async def _bulk_loop(self, family: IPFamily, worker: int) -> None:
        targets = tuple(
            target for target in self._config.bulk.targets if target.family in (family, "both")
        )
        index = worker % len(targets) if targets else 0
        while True:
            if not targets or not self._is_online(family):
                await self._sleep(self._request_interval)
                continue
            for offset in range(len(targets)):
                chosen = (index + offset) % len(targets)
                target = targets[chosen]
                state = self._states[target.url]
                if self._cooling(state, family) or state.next_request > self._clock():
                    continue
                # Atomic on this event loop, before the first request await.
                state.next_request = self._clock() + self._request_interval
                index = (chosen + 1) % len(targets)
                await self._fetch(family, target, state, bulk=True)
                break
            # Also wait when every source fails; there is no traffic debt to repay.
            await self._sleep(self._request_interval)

    def _retry_after(self, value: str | None) -> float | None:
        if value is None:
            return None
        value = value.strip()
        if re.fullmatch(r"[0-9]+", value):
            digits = value.lstrip("0") or "0"
            # Avoid both integer-conversion limits and overflowing event-loop timers.
            return math.inf if len(digits) > 5 else float(digits)
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            return max(0.0, date.timestamp() - self._wall_clock())
        except (ValueError, OverflowError, TypeError):
            return None

    def _failure(
        self,
        state: _TargetState,
        stats: _Stats,
        reason: str,
        *,
        status: int | None = None,
        retry_after: str | None = None,
    ) -> None:
        stats.failures += 1
        stats.last_result = reason
        state.failures += 1
        state.version += 1
        delay = min(
            300.0, (30.0 if status in (403, 404) else 5.0) * 2 ** min(6, state.failures - 1)
        )
        if status == 429:
            server_delay = self._retry_after(retry_after)
            if server_delay is not None:
                if server_delay > 86400:
                    state.paused = True
                    stats.last_result = "retry_after_too_large"
                else:
                    delay = max(delay, server_delay)
        state.until = max(state.until, self._clock() + delay)
        if status == 429:
            state.throttled_until = max(state.throttled_until, state.until)

    def _transport_failure(
        self, state: _TargetState, stats: _Stats, family: IPFamily, reason: str
    ) -> None:
        # DNS/routes/timeouts on one address family are not server-wide throttles.
        stats.failures += 1
        stats.last_result = reason
        local = state.transport[family]
        local.failures += 1
        local.version += 1
        local.until = max(
            local.until, self._clock() + min(300.0, 5.0 * 2 ** min(6, local.failures - 1))
        )

    @staticmethod
    def _range_length(response: aiohttp.ClientResponse, limit: int) -> int | None:
        match = re.fullmatch(
            r"bytes ([0-9]{1,20})-([0-9]{1,20})/([0-9]{1,20})",
            response.headers.get("Content-Range", ""),
        )
        if match is None:
            return None
        start, end, total = map(int, match.groups())
        if start != 0 or not start <= end < limit or total <= end:
            return None
        length = end + 1
        if response.content_length is not None and response.content_length != length:
            return None
        return length

    async def _fetch(
        self, family: IPFamily, target: Target, state: _TargetState, *, bulk: bool
    ) -> None:
        stats = self._stats["bulk" if bulk else "probe"][family]
        stats.requests += 1
        stats.last_status = None
        # aiohttp can assert internally for a literal IP with the wrong connector
        # family. Treat that unavailable stack explicitly, without catching bugs.
        try:
            literal = ip_address(urlsplit(target.url).hostname or "")
        except ValueError:
            literal = None
        if literal is not None and literal.version != (4 if family == "ipv4" else 6):
            self._transport_failure(state, stats, family, "network_error")
            return
        version = state.version
        transport_version = state.transport[family].version
        limit = (
            self._config.bulk.max_response_bytes if bulk else self._config.probe.max_response_bytes
        )
        headers = {"Accept-Encoding": "identity"}
        if bulk:
            headers["Range"] = f"bytes=0-{limit - 1}"
        # Allow a complete fair turn for every worker, including very low quotas.
        workers = max(1, sum(self._workers[f] for f in self._families))
        total_timeout = self._connect_timeout + self._read_timeout
        if bulk:
            total_timeout += 2 * limit * workers / self._limiter.rate
        session = self._sessions[family]
        if session.closed:
            self._transport_failure(state, stats, family, "session_unavailable")
            return
        try:
            async with asyncio.timeout(total_timeout):
                async with (
                    asyncio.timeout(self._connect_timeout + self._read_timeout) as header_timeout,
                    session.get(
                        target.url,
                        headers=headers,
                        allow_redirects=False,
                        auto_decompress=False,
                        read_bufsize=min(16384, limit),
                        timeout=aiohttp.ClientTimeout(
                            total=None,
                            connect=self._connect_timeout,
                            sock_connect=self._connect_timeout,
                            sock_read=None,
                        ),
                    ) as response,
                ):
                    header_timeout.reschedule(None)
                    stats.last_status = response.status
                    if 300 <= response.status < 400:
                        stats.redirects += 1
                    if (bulk and response.status not in (200, 206)) or (
                        not bulk and not 200 <= response.status < 300
                    ):
                        self._failure(
                            state,
                            stats,
                            "http_status",
                            status=response.status,
                            retry_after=response.headers.get("Retry-After"),
                        )
                        return
                    expected = None
                    if bulk and response.status == 206:
                        expected = self._range_length(response, limit)
                        if expected is None:
                            self._failure(state, stats, "invalid_range")
                            return
                        limit = expected
                    if bulk and response.content_type in ("text/html", "application/xhtml+xml"):
                        self._failure(state, stats, "html")
                        return
                    if (
                        bulk
                        and response.headers.get("Content-Encoding", "identity").lower()
                        != "identity"
                    ):
                        self._failure(state, stats, "content_encoding")
                        return
                    outcome = await self._read_body(
                        response, family, state, stats, limit, bulk, expected
                    )
                    stats.last_result = outcome
                    if outcome == "ok":
                        stats.successes += 1
                        # A concurrent success must not erase a newer server failure.
                        if version == state.version:
                            state.failures = 0
                        if transport_version == state.transport[family].version:
                            state.transport[family].failures = 0
                    elif outcome in ("offline", "cooling"):
                        stats.aborted += 1
                    else:
                        self._failure(state, stats, outcome)
        except (aiohttp.ClientError, OSError, TimeoutError):
            # Never stringify exceptions: aiohttp errors can contain secret URLs.
            self._transport_failure(state, stats, family, "network_error")

    async def _read_body(
        self,
        response: aiohttp.ClientResponse,
        family: IPFamily,
        state: _TargetState,
        stats: _Stats,
        limit: int,
        bulk: bool,
        expected: int | None,
    ) -> str:
        received = 0
        prefix = bytearray()
        while received < limit:
            if response.content.at_eof():
                break
            if bulk and not self._is_online(family):
                return "offline"
            if bulk and self._cooling(state, family):
                return "cooling"
            # aiohttp raises its watermarks when read(n) exceeds the low watermark.
            amount = min(limit - received, 16384, self._limiter.burst_bytes if bulk else 16384)
            generation = await self._limiter.acquire(amount) if bulk else 0
            used = 0
            try:
                if bulk and not self._is_online(family):
                    return "offline"
                if bulk and self._cooling(state, family):
                    return "cooling"
                # aiohttp's sock_read timer is disabled: budget waiting is not I/O.
                async with asyncio.timeout(self._read_timeout):
                    data = await response.content.read(amount)
                used = len(data)
                stats.bytes += used
                received += used
            finally:
                if bulk:
                    self._limiter.refund(amount - used, generation=generation)
            if not data:
                break
            if bulk:
                prefix.extend(data[: max(0, 512 - len(prefix))])
                if _looks_like_html(bytes(prefix)):
                    return "html"
        if received == 0:
            return "empty" if bulk else "ok"
        if expected is not None and received != expected:
            return "truncated_range"
        return "ok"
