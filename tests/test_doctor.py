"""One-shot diagnostics using synthetic targets and loopback servers only."""

from __future__ import annotations

import asyncio
import errno
import gzip
import socket
import ssl
from contextlib import AsyncExitStack

import aiohttp
import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver
from aiohttp.client_reqrep import ConnectionKey
from yarl import URL

import thuauth.doctor as doctor
from thuauth.config import AuthConfig, BulkConfig, Config, ProbeConfig
from thuauth.http import SessionPool
from thuauth.models import Credentials, Target

SECRET = "SyntheticPrivateDoctorValue123"


def configuration(probe=(), bulk=(), *, ipv4=True, ipv6=False, enabled=False):
    return Config(
        credentials=Credentials(SECRET, SECRET),
        auth=AuthConfig(ipv4=ipv4, ipv6=ipv6),
        probe=ProbeConfig(targets=tuple(probe)),
        bulk=BulkConfig(enabled=enabled, workers_ipv4=0, workers_ipv6=0, targets=tuple(bulk)),
    )


class LoopbackResolver(AbstractResolver):
    def __init__(self, base):
        self.base = URL(base)
        self.calls = []

    async def resolve(self, host, port=0, family=socket.AF_INET):
        assert host == "diagnostic.test"
        self.calls.append(family)
        return [
            {
                "hostname": host,
                "host": self.base.host,
                "port": self.base.port,
                "family": family,
                "proto": socket.IPPROTO_TCP,
                "flags": 0,
            }
        ]

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_applicable_families_indices_and_disabled_bulk_are_checked_once(http_server):
    requests = []

    async def handler(request):
        requests.append((request.path, request.transport.get_extra_info("socket").family))
        assert request.method == "GET" and not request.query
        assert request.headers["Range"] == "bytes=0-1023"
        assert request.headers["Accept-Encoding"] == "identity"
        return web.Response(body=b"small response")

    app4, app6 = web.Application(), web.Application()
    app4.router.add_get("/{path:.*}", handler)
    app6.router.add_get("/{path:.*}", handler)
    base4 = await http_server(app4)
    base6 = await http_server(app6, socket.AF_INET6)
    config = configuration(
        (
            Target("http://diagnostic.test/probe", "both"),
            Target(base4 + "/only4"),
            Target(base6 + "/only6", "ipv6"),
        ),
        (Target("http://diagnostic.test/bulk", "both"),),
        ipv6=True,
    )
    async with AsyncExitStack() as stack:
        sessions = {}
        for family, socket_family, base in (
            ("ipv4", socket.AF_INET, base4),
            ("ipv6", socket.AF_INET6, base6),
        ):
            sessions[family] = await stack.enter_async_context(
                aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(
                        family=socket_family, resolver=LoopbackResolver(base)
                    ),
                    cookie_jar=aiohttp.DummyCookieJar(),
                    trust_env=False,
                )
            )
        results = await doctor.diagnose_targets(config, sessions)
        assert all(not session.closed for session in sessions.values())
    assert results == [
        {"kind": kind, "index": index, "family": family, "status": 200, "result": "ok"}
        for kind, index, family in (
            ("probe", 0, "ipv4"),
            ("probe", 0, "ipv6"),
            ("probe", 1, "ipv4"),
            ("probe", 2, "ipv6"),
            ("bulk", 0, "ipv4"),
            ("bulk", 0, "ipv6"),
        )
    ]
    assert requests == [
        ("/probe", socket.AF_INET),
        ("/probe", socket.AF_INET6),
        ("/only4", socket.AF_INET),
        ("/only6", socket.AF_INET6),
        ("/bulk", socket.AF_INET),
        ("/bulk", socket.AF_INET6),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_disabled_stack_is_skipped_but_bulk_enabled_and_worker_count_do_not_gate_diagnosis(
    http_server, enabled
):
    paths = []

    async def handler(request):
        paths.append(request.path)
        return web.Response()

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    base = await http_server(app)
    config = configuration(
        (Target(base + "/skip", "ipv6"), Target(base + "/probe", "both")),
        (Target(base + "/bulk", "both"),),
        enabled=enabled,
    )
    async with SessionPool(("ipv4",)) as pool:
        results = await doctor.diagnose_targets(config, pool.traffic)
    assert paths == ["/probe", "/bulk"]
    assert [(r["kind"], r["index"], r["family"]) for r in results] == [
        ("probe", 1, "ipv4"),
        ("bulk", 0, "ipv4"),
    ]
    assert await doctor.diagnose_targets(configuration(), {}) == []


@pytest.mark.asyncio
async def test_no_credentials_cookies_headers_proxy_or_decompression_are_inherited(
    http_server, monkeypatch
):
    compressed = gzip.compress(b"x" * 100000)
    requests, read_bodies = [], []
    original_read = aiohttp.StreamReader.read
    original_get = aiohttp.ClientSession.get

    async def record_read(self, n=-1):
        assert 0 < n <= 1024
        body = await original_read(self, n)
        read_bodies.append(body)
        return body

    def inspect(self, *args, **kwargs):
        assert not self.auto_decompress and not self.trust_env
        assert isinstance(self.cookie_jar, aiohttp.DummyCookieJar)
        assert self.timeout.total == 5
        assert self._read_bufsize == 1024
        assert kwargs["allow_redirects"] is False
        assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED and kwargs["ssl"].check_hostname
        return original_get(self, *args, **kwargs)

    async def handler(request):
        requests.append(dict(request.headers))
        response = web.Response(body=compressed, headers={"Content-Encoding": "gzip"})
        response.set_cookie("diagnostic-cookie", SECRET)
        return response

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    base = await http_server(app)
    monkeypatch.setattr(aiohttp.StreamReader, "read", record_read)
    monkeypatch.setattr(aiohttp.ClientSession, "get", inspect)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    async with SessionPool(("ipv4",)) as pool:
        # Even accidentally passing auth sessions must not send their credentials.
        session = pool.auth["ipv4"]
        session.cookie_jar.update_cookies({"auth": SECRET})
        session.headers["Authorization"] = "Bearer " + SECRET
        session.headers["X-Private"] = SECRET
        results = await doctor.diagnose_targets(
            configuration((Target(base + "/probe"),), (Target(base + "/bulk"),)), pool.auth
        )
        assert not session.closed and session.headers["X-Private"] == SECRET
    assert len(requests) == 2 and all(r["result"] == "ok" for r in results)
    assert all(
        "Cookie" not in h and "Authorization" not in h and "X-Private" not in h for h in requests
    )
    assert b"".join(read_bodies) == compressed * 2
    assert SECRET not in repr(results) and base not in repr(results)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 206])
async def test_range_is_bounded_even_when_ignored_and_response_is_closed(
    http_server, monkeypatch, status
):
    release = asyncio.Event()
    transports, read_sizes = [], []
    original_read = aiohttp.StreamReader.read

    async def measured(self, n=-1):
        assert 0 < n <= 1024
        data = await original_read(self, n)
        read_sizes.append(len(data))
        return data

    async def handler(request):
        assert request.headers["Range"] == "bytes=0-1023"
        transports.append(request.transport)
        response = web.StreamResponse(status=status, headers={"Content-Length": "1000000"})
        await response.prepare(request)
        await response.write(b"x" * 16384)
        await release.wait()
        return response

    app = web.Application()
    app.router.add_get("/", handler)
    base = await http_server(app)
    monkeypatch.setattr(aiohttp.StreamReader, "read", measured)
    try:
        async with SessionPool(("ipv4",)) as pool, asyncio.timeout(1):
            result = await doctor.diagnose_targets(configuration((Target(base),)), pool.traffic)
            assert result == [
                {"kind": "probe", "index": 0, "family": "ipv4", "status": status, "result": "ok"}
            ]
            assert not pool.traffic["ipv4"].connector._acquired
            while not transports[0].is_closing():
                await asyncio.sleep(0.005)
        assert sum(read_sizes) == 1024
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 401, 403, 404, 429, 500, 503])
async def test_redirect_and_http_errors_are_not_followed_or_read_and_other_targets_continue(
    http_server, status, caplog
):
    paths = []

    async def handler(request):
        paths.append(request.path)
        if request.path == "/error":
            return web.Response(
                status=status,
                body=SECRET.encode(),
                headers={"Location": "/login?password=" + SECRET},
            )
        return web.Response()

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    base = await http_server(app)
    async with SessionPool(("ipv4",)) as pool:
        results = await doctor.diagnose_targets(
            configuration((Target(base + "/error"), Target(base + "/ok"))), pool.traffic
        )
    assert paths == ["/error", "/ok"]
    assert results == [
        {
            "kind": "probe",
            "index": 0,
            "family": "ipv4",
            "status": status,
            "result": "redirect" if status < 400 else "http",
        },
        {"kind": "probe", "index": 1, "family": "ipv4", "status": 200, "result": "ok"},
    ]
    assert SECRET not in repr(results) + caplog.text and base not in repr(results)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["headers", "body"])
async def test_timeout_is_local_and_other_targets_continue(http_server, monkeypatch, phase):
    release = asyncio.Event()
    original_timeout = doctor._timeout
    budgets = []

    def shorter(seconds):
        budgets.append(seconds)
        return original_timeout(0.05)

    monkeypatch.setattr(doctor, "_timeout", shorter)

    async def handler(request):
        if request.path == "/ok":
            return web.Response()
        response = web.StreamResponse()
        if phase == "body":
            await response.prepare(request)
            await response.write(SECRET.encode())
        await release.wait()
        return response

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    base = await http_server(app)
    try:
        async with SessionPool(("ipv4",)) as pool:
            results = await doctor.diagnose_targets(
                configuration((Target(base + "/slow"), Target(base + "/ok"))), pool.traffic
            )
            assert not pool.traffic["ipv4"].connector._acquired
        assert results[0]["status"] == (None if phase == "headers" else 200)
        assert [r["result"] for r in results] == ["timeout", "ok"]
        assert budgets == [5, 5]
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["headers", "body"])
async def test_disconnection_never_replays_a_request(http_server, phase):
    requests = []

    async def handler(request):
        requests.append(request.path)
        if phase == "body":
            response = web.StreamResponse(headers={"Content-Length": "1000"})
            await response.prepare(request)
            await response.write(SECRET.encode())
        else:
            response = web.Response()
        request.transport.abort()
        return response

    app = web.Application()
    app.router.add_get("/", handler)
    base = await http_server(app)
    async with SessionPool(("ipv4",)) as pool:
        results = await doctor.diagnose_targets(configuration((Target(base),)), pool.traffic)
        assert not pool.traffic["ipv4"].connector._acquired
    assert requests == ["/"]
    assert results[0]["result"] == "connection"
    assert results[0]["status"] == (None if phase == "headers" else 200)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["headers", "body"])
async def test_cancellation_propagates_and_stops_further_requests(http_server, phase):
    ready, release = asyncio.Event(), asyncio.Event()
    paths = []

    async def handler(request):
        paths.append(request.path)
        response = web.StreamResponse()
        if phase == "body":
            await response.prepare(request)
            await response.write(b"x")
        ready.set()
        await release.wait()
        return response

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    base = await http_server(app)
    try:
        async with SessionPool(("ipv4",)) as pool:
            task = asyncio.create_task(
                doctor.diagnose_targets(
                    configuration((Target(base + "/first"), Target(base + "/second"))), pool.traffic
                )
            )
            await asyncio.wait_for(ready.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not pool.traffic["ipv4"].connector._acquired
            assert not pool.traffic["ipv4"].closed
        assert paths == ["/first"]
    finally:
        release.set()


def connector_error(error, *, dns=False):
    key = ConnectionKey(SECRET, 443, True, True, None, None, None)
    return (aiohttp.ClientConnectorDNSError if dns else aiohttp.ClientConnectorError)(key, error)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (socket.gaierror(socket.EAI_NONAME, SECRET), "dns"),
        (connector_error(OSError(errno.ENETUNREACH, SECRET), dns=True), "dns"),
        (connector_error(OSError(errno.ENETUNREACH, SECRET)), "unavailable"),
        (TimeoutError(SECRET), "timeout"),
        (ssl.SSLCertVerificationError(1, SECRET), "tls"),
        (aiohttp.ClientPayloadError(SECRET), "connection"),
    ],
    ids=lambda item: item if isinstance(item, str) else type(item).__name__,
)
async def test_expected_errors_use_fixed_classifications(monkeypatch, error, expected, caplog):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(aiohttp.ClientSession, "get", fail)
    async with SessionPool(("ipv4",)) as pool:
        results = await doctor.diagnose_targets(
            configuration((Target("http://127.0.0.1/" + SECRET),)), pool.traffic
        )
    assert results == [
        {"kind": "probe", "index": 0, "family": "ipv4", "status": None, "result": expected}
    ]
    assert SECRET not in repr(results) + caplog.text


@pytest.mark.asyncio
async def test_programming_errors_and_missing_sessions_are_not_swallowed(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic coding bug")

    config = configuration((Target("http://127.0.0.1/"),))
    monkeypatch.setattr(aiohttp.ClientSession, "get", fail)
    async with SessionPool(("ipv4",)) as pool:
        with pytest.raises(RuntimeError, match="synthetic coding bug"):
            await doctor.diagnose_targets(config, pool.traffic)
    with pytest.raises(KeyError):
        await doctor.diagnose_targets(config, {})
