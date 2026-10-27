"""Account-wide authentication coordination; observations never imply retries."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Protocol

from thuauth.config import Config
from thuauth.errors import NetworkError, ProtocolError
from thuauth.models import (
    AccessContext,
    AuthReply,
    AuthReplyKind,
    Credentials,
    IPFamily,
    OnlineObservation,
    OnlineState,
)

logger = logging.getLogger(__name__)


class AuthClient(Protocol):
    async def discover(self) -> AccessContext: ...
    async def status(self, access: AccessContext | None = None) -> OnlineObservation: ...
    async def login(self, credentials: Credentials, access: AccessContext) -> AuthReply: ...
    async def logout(self, username: str, access: AccessContext) -> AuthReply: ...


def enabled_families(config: Config) -> tuple[IPFamily, ...]:
    families: list[IPFamily] = []
    if config.auth.ipv4:
        families.append("ipv4")
    if config.auth.ipv6:
        families.append("ipv6")
    return tuple(families)


class AuthService:
    def __init__(
        self,
        config: Config,
        clients: Mapping[IPFamily, AuthClient],
        *,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.config = config
        self.clients = clients
        self.families = enabled_families(config)
        self._clock = clock
        self._jitter = jitter
        self._lock = asyncio.Lock()
        self._generation = 0
        self._blocked: str | None = None
        self._failures = 0
        self._next_attempt = 0.0
        self._next_family = 0
        self._states = {
            family: OnlineObservation(family, OnlineState.UNKNOWN, reason="not_checked")
            for family in self.families
        }

    @property
    def observations(self) -> tuple[OnlineObservation, ...]:
        return tuple(self._states[family] for family in self.families)

    def is_online(self, family: IPFamily) -> bool:
        observation = self._states.get(family)
        return bool(
            observation
            and observation.state is OnlineState.ONLINE
            and self.config.credentials.username
            and observation.username == self.config.credentials.username
            and self._clock() - observation.observed_at
            <= max(30.0, 2 * self.config.auth.health_interval_seconds)
        )

    def invalidate(self) -> None:
        """Synchronously invalidate in-flight results while preserving account cooldowns."""
        self._generation += 1
        self._states = {
            family: OnlineObservation(
                family,
                OnlineState.BLOCKED if self._blocked else OnlineState.UNKNOWN,
                reason=self._blocked or "network_changed",
                observed_at=self._clock(),
            )
            for family in self.families
        }

    async def _observe(self, family: IPFamily) -> OnlineObservation:
        try:
            # An online network may no longer intercept the discovery URL.
            # Read-only status must not depend on discovering a login ac_id.
            observation = await self.clients[family].status()
        except NetworkError as exc:
            state = OnlineState.UNAVAILABLE if exc.kind == "unavailable" else OnlineState.UNKNOWN
            observation = OnlineObservation(family, state, reason=exc.kind)
        except ProtocolError:
            observation = OnlineObservation(family, OnlineState.UNKNOWN, reason="protocol")
        return replace(observation, observed_at=self._clock())

    async def _observe_all(self, generation: int) -> bool:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(self._observe(family)) for family in self.families]
        results = [task.result() for task in tasks]
        if generation != self._generation:
            return False
        username = self.config.credentials.username
        for observation in results:
            if (
                observation.state is OnlineState.ONLINE
                and username
                and observation.username != username
            ):
                self._blocked = "account_conflict"
                break
        for observation in results:
            if self._blocked and (
                observation.state is not OnlineState.ONLINE or observation.username != username
            ):
                observation = replace(observation, state=OnlineState.BLOCKED, reason=self._blocked)
            previous = self._states[observation.family]
            if previous.state != observation.state or previous.reason != observation.reason:
                logger.info(
                    "auth_state family=%s state=%s reason=%s",
                    observation.family,
                    observation.state.value,
                    observation.reason,
                )
            self._states[observation.family] = observation
        return True

    def _cooldown(self) -> None:
        self._failures = min(self._failures + 1, 6)
        delay = min(600.0, 30 * 2 ** (self._failures - 1) * (1 + 0.2 * self._jitter()))
        self._next_attempt = self._clock() + delay

    async def status_all(self) -> tuple[OnlineObservation, ...]:
        async with self._lock:
            await self._observe_all(self._generation)
            return self.observations

    async def ensure_all(self) -> tuple[OnlineObservation, ...]:
        async with self._lock:
            generation = self._generation
            if not await self._observe_all(generation):
                return self.observations
            candidates = self.families[self._next_family :] + self.families[: self._next_family]
            for family in candidates:
                if self._blocked or self._clock() < self._next_attempt:
                    break
                if self._states[family].state is not OnlineState.OFFLINE:
                    continue
                self.config.require_credentials()
                # Preserve account cooldown, but let the other stack try next.
                self._next_family = (self.families.index(family) + 1) % len(self.families)
                self._cooldown()
                reply: AuthReply | None = None
                try:
                    # Refresh access/challenge for every operation, even after a status query.
                    access = await self.clients[family].discover()
                    if generation != self._generation:
                        return self.observations
                    reply = await self.clients[family].login(self.config.credentials, access)
                except (NetworkError, ProtocolError):
                    # The portal may have processed a request whose response was lost.
                    pass
                if generation != self._generation:
                    return self.observations
                if reply is not None:
                    if reply.kind is AuthReplyKind.BLOCKED:
                        self._blocked = "credentials_or_account_blocked"
                    elif reply.kind is AuthReplyKind.REJECTED:
                        self._blocked = "auth_rejected"
                if not await self._observe_all(generation):
                    return self.observations
                if (
                    not self._blocked
                    and self._states[family].state is OnlineState.ONLINE
                    and (reply is None or reply.kind is not AuthReplyKind.THROTTLED)
                ):
                    self._failures = 0
                    self._next_attempt = 0
            return self.observations

    async def logout_all(self) -> tuple[OnlineObservation, ...]:
        async with self._lock:
            generation = self._generation
            if not await self._observe_all(generation):
                return self.observations
            username = self.config.credentials.username
            for family in self.families:
                observation = self._states[family]
                if (
                    not username
                    or observation.state is not OnlineState.ONLINE
                    or observation.username != username
                ):
                    continue
                try:
                    access = await self.clients[family].discover()
                    if generation != self._generation:
                        return self.observations
                    await self.clients[family].logout(username, access)
                except (NetworkError, ProtocolError):
                    pass
                if not await self._observe_all(generation):
                    return self.observations
            return self.observations
