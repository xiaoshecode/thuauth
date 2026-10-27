"""State-machine tests use synthetic clients and an injected monotonic clock."""

import asyncio
from dataclasses import replace

import pytest

from thuauth.auth import AuthService
from thuauth.config import AuthConfig, Config
from thuauth.errors import ConfigError, NetworkError, ProtocolError
from thuauth.models import (
    AccessContext,
    AuthReply,
    AuthReplyKind,
    Credentials,
    OnlineObservation,
    OnlineState,
)


class Portal:
    def __init__(self, *, double_stack=False):
        self.owners = {"ipv4": None, "ipv6": None}
        self.status_errors = {}
        self.login_error = None
        self.reply = AuthReplyKind.ACCEPTED
        self.double_stack = double_stack
        self.logins = []
        self.logouts = []
        self.discoveries = []

    def client(self, family):
        backend = self

        class Client:
            async def discover(self):
                backend.discoveries.append(family)
                return AccessContext(family, "1")

            async def status(self, access=None):
                if error := backend.status_errors.get(family):
                    raise error
                owner = backend.owners[family]
                return OnlineObservation(
                    family, OnlineState.ONLINE if owner else OnlineState.OFFLINE, owner
                )

            async def login(self, credentials, access):
                backend.logins.append(family)
                if backend.reply is AuthReplyKind.ACCEPTED:
                    for target in backend.owners if backend.double_stack else (family,):
                        backend.owners[target] = credentials.username
                if backend.login_error:
                    raise backend.login_error
                return AuthReply(backend.reply, None, "synthetic reply")

            async def logout(self, username, access):
                backend.logouts.append(family)
                for target in backend.owners if backend.double_stack else (family,):
                    backend.owners[target] = None
                return AuthReply(AuthReplyKind.ACCEPTED, None, "synthetic reply")

        return Client()


def make_service(portal, *, config=None, now=None):
    config = config or Config(credentials=Credentials("synthetic-user", "synthetic-password"))
    now = now or [0.0]
    return AuthService(
        config,
        {family: portal.client(family) for family in ("ipv4", "ipv6")},
        clock=lambda: now[0],
        jitter=lambda: 0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("double_stack", [False, True])
async def test_login_rechecks_linked_families_without_duplicate_auth(double_stack):
    portal = Portal(double_stack=double_stack)
    service = make_service(portal)
    results = await service.ensure_all()
    assert all(item.state is OnlineState.ONLINE for item in results)
    assert portal.logins == (["ipv4"] if double_stack else ["ipv4", "ipv6"])
    assert service.is_online("ipv4") and service.is_online("ipv6")
    assert portal.discoveries == portal.logins


@pytest.mark.asyncio
async def test_concurrent_ensure_is_serialized_and_rechecks_state():
    portal = Portal(double_stack=True)
    service = make_service(portal)
    await asyncio.gather(*(service.ensure_all() for _ in range(8)))
    assert portal.logins == ["ipv4"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [NetworkError("timeout"), NetworkError("dns"), ProtocolError("invalid_response")]
)
async def test_unknown_never_causes_password_submission(error):
    portal = Portal()
    portal.status_errors = {"ipv4": error, "ipv6": error}
    results = await make_service(portal).ensure_all()
    assert all(item.state is OnlineState.UNKNOWN for item in results)
    assert portal.logins == []


@pytest.mark.asyncio
async def test_ipv6_unavailable_does_not_stop_ipv4():
    portal = Portal()
    portal.status_errors["ipv6"] = NetworkError("unavailable")
    results = await make_service(portal).ensure_all()
    assert [item.state for item in results] == [OnlineState.ONLINE, OnlineState.UNAVAILABLE]
    assert portal.logins == ["ipv4"]


@pytest.mark.asyncio
async def test_other_account_is_not_kicked_or_replaced():
    portal = Portal()
    portal.owners["ipv4"] = "other-synthetic-user"
    service = make_service(portal)
    results = await service.ensure_all()
    assert all(item.state is OnlineState.BLOCKED for item in results)
    await service.logout_all()
    assert not portal.logins and not portal.logouts
    assert not service.is_online("ipv4")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [AuthReplyKind.BLOCKED, AuthReplyKind.REJECTED])
async def test_refusal_blocks_both_families_and_survives_network_reset(kind):
    portal = Portal()
    portal.reply = kind
    now = [0.0]
    service = make_service(portal, now=now)
    await service.ensure_all()
    service.invalidate()
    now[0] = 10000
    results = await service.ensure_all()
    assert portal.logins == ["ipv4"]
    assert all(item.state is OnlineState.BLOCKED for item in results)


@pytest.mark.asyncio
async def test_throttle_is_account_wide_and_retries_only_after_cooldown():
    portal = Portal()
    portal.reply = AuthReplyKind.THROTTLED
    now = [0.0]
    service = make_service(portal, now=now)
    await service.ensure_all()
    now[0] = 29
    service.invalidate()
    await service.ensure_all()
    assert portal.logins == ["ipv4"]
    now[0] = 30
    portal.reply = AuthReplyKind.ACCEPTED
    await service.ensure_all()
    assert portal.logins == ["ipv4", "ipv6", "ipv4"]


@pytest.mark.asyncio
async def test_lost_login_response_rechecks_before_any_retry():
    portal = Portal(double_stack=True)
    portal.login_error = NetworkError("timeout")
    service = make_service(portal)
    results = await service.ensure_all()
    assert all(item.state is OnlineState.ONLINE for item in results)
    assert portal.logins == ["ipv4"]


@pytest.mark.asyncio
async def test_already_online_reply_is_not_online_proof():
    portal = Portal()
    portal.reply = AuthReplyKind.ALREADY_ONLINE
    results = await make_service(portal).ensure_all()
    assert all(item.state is OnlineState.OFFLINE for item in results)
    assert portal.logins == ["ipv4"]


@pytest.mark.asyncio
async def test_missing_credentials_cannot_reach_login():
    portal = Portal()
    service = make_service(portal, config=Config())
    with pytest.raises(ConfigError):
        await service.ensure_all()
    assert not portal.logins


@pytest.mark.asyncio
async def test_logout_rechecks_double_stack_and_never_needs_password():
    portal = Portal(double_stack=True)
    portal.owners = {"ipv4": "synthetic-user", "ipv6": "synthetic-user"}
    config = Config(credentials=Credentials("synthetic-user", ""))
    results = await make_service(portal, config=config).logout_all()
    assert all(item.state is OnlineState.OFFLINE for item in results)
    assert portal.logouts == ["ipv4"]
    assert not portal.logins


@pytest.mark.asyncio
async def test_status_is_read_only_and_online_observations_expire():
    portal = Portal()
    portal.owners["ipv4"] = "synthetic-user"
    now = [0.0]
    service = make_service(portal, now=now)
    await service.status_all()
    assert service.is_online("ipv4")
    now[0] = 61
    assert not service.is_online("ipv4")
    assert not portal.logins


@pytest.mark.asyncio
async def test_stale_response_cannot_overwrite_network_invalidation():
    portal = Portal()
    service = make_service(portal)
    entered, release = asyncio.Event(), asyncio.Event()
    client = service.clients["ipv4"]
    original = client.status

    async def delayed(access=None):
        entered.set()
        await release.wait()
        return await original(access)

    client.status = delayed
    task = asyncio.create_task(service.ensure_all())
    await entered.wait()
    service.invalidate()
    release.set()
    await task
    assert all(item.reason == "network_changed" for item in service.observations)
    assert not portal.logins


@pytest.mark.asyncio
async def test_programming_errors_propagate_and_cancel_sibling_observation():
    portal = Portal()
    service = make_service(portal)
    cancelled = asyncio.Event()

    async def broken(access=None):
        await asyncio.sleep(0)
        raise RuntimeError("synthetic programming failure")

    async def waiting(access=None):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service.clients["ipv4"].status = broken
    service.clients["ipv6"].status = waiting
    with pytest.raises(ExceptionGroup):
        await service.status_all()
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_cancellation_releases_authentication_lock():
    portal = Portal()
    service = make_service(portal)
    original = service.clients["ipv4"].status
    entered = asyncio.Event()

    async def waiting(access=None):
        entered.set()
        await asyncio.Event().wait()

    service.clients["ipv4"].status = waiting
    task = asyncio.create_task(service.ensure_all())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    service.clients["ipv4"].status = original
    await asyncio.wait_for(service.ensure_all(), 1)
    assert portal.logins == ["ipv4", "ipv6"]


@pytest.mark.asyncio
async def test_disabled_family_is_not_contacted():
    portal = Portal()
    config = replace(
        Config(credentials=Credentials("synthetic-user", "synthetic-password")),
        auth=AuthConfig(ipv6=False),
    )
    results = await make_service(portal, config=config).ensure_all()
    assert len(results) == 1
    assert "ipv6" not in portal.discoveries


@pytest.mark.asyncio
async def test_online_status_does_not_depend_on_login_discovery():
    portal = Portal()
    portal.owners = {"ipv4": "synthetic-user", "ipv6": "synthetic-user"}
    service = make_service(portal)

    async def unavailable_discovery():
        raise ProtocolError("invalid_response")

    for client in service.clients.values():
        client.discover = unavailable_discovery
    assert all(item.state is OnlineState.ONLINE for item in await service.ensure_all())
    assert not portal.logins


@pytest.mark.asyncio
async def test_failed_ipv4_does_not_starve_ipv6_after_account_cooldown():
    portal = Portal()
    now = [0.0]
    service = make_service(portal, now=now)

    async def failed_ipv4(credentials, access):
        portal.logins.append("ipv4")
        raise NetworkError("timeout")

    service.clients["ipv4"].login = failed_ipv4
    await service.ensure_all()
    assert portal.logins == ["ipv4"]
    now[0] = 29
    await service.ensure_all()
    assert portal.logins == ["ipv4"]
    service.invalidate()  # Recovery must preserve both cooldown and fair turn.
    now[0] = 30
    await service.ensure_all()
    assert portal.logins[:2] == ["ipv4", "ipv6"]
    assert service.is_online("ipv6")
