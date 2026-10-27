"""Windows loopback transport tests; every account/token/response is synthetic."""

from __future__ import annotations

import asyncio
import errno
import gzip
import json
import socket
import ssl
import traceback
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.client_reqrep import ConnectionKey

from thuauth.errors import NetworkError, ProtocolError
from thuauth.http import SessionPool, SrunClient
from thuauth.models import AccessContext, AuthReplyKind, Credentials, OnlineState
from thuauth.protocol.srun import (
    CALLBACK,
    MAX_RESPONSE_BYTES,
    build_login_params,
    build_logout_params,
)

SECRET = "SyntheticPrivateValue123"
USER = "synthetic-user"
TOKEN = "synthetic-challenge-token-0123456789abcdef"
ACCESS = AccessContext("ipv4", "01")
VECTORS = json.loads(
    (Path(__file__).parent / "fixtures/go_protocol_vectors.json").read_text(encoding="utf-8")
)["requests"]


def jsonp(value, callback=CALLBACK):
    return f"{callback}({json.dumps(value)})".encode()


def client_for(pool, base, *, family="ipv4", **kwargs):
    return SrunClient(
        family,
        lambda: pool.auth[family],
        base_url=base,
        discovery_url=base + "/discover",
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", VECTORS, ids=lambda case: case["name"])
async def test_actual_wire_parameters_match_go_vectors(http_server, case):
    seen = []

    async def handler(request):
        seen.append((request.path, dict(request.query)))
        if request.path == "/cgi-bin/get_challenge":
            return web.Response(
                body=jsonp({"res": "ok", "challenge": case["token"], "client_ip": "192.0.2.99"})
            )
        return web.Response(body=jsonp({"error": "ok", "ecode": 0}))

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    base = await http_server(app)
    async with SessionPool(("ipv4",)) as pool:
        client = client_for(pool, base)
        access = AccessContext("ipv4", case["ac_id"], case["ip"])
        if case["logout"]:
            reply = await client.logout(case["username"], access)
        else:
            reply = await client.login(Credentials(case["username"], case["password"]), access)
        assert reply.kind == AuthReplyKind.ACCEPTED
    assert seen == [
        ("/cgi-bin/get_challenge", {**case["challenge_params"], "callback": CALLBACK}),
        ("/cgi-bin/srun_portal", {**case["params"], "callback": CALLBACK}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["ipv4", "ipv6"])
async def test_fresh_challenge_and_real_address_family(http_server, family):
    challenges = []
    operations = []
    peers = []

    async def handler(request):
        peers.append(request.transport.get_extra_info("socket").family)
        if request.path == "/cgi-bin/get_challenge":
            challenges.append(dict(request.query))
            return web.Response(
                body=jsonp(
                    {"res": "ok", "challenge": TOKEN + str(len(challenges)), "client_ip": "::1"}
                )
            )
        operations.append(dict(request.query))
        return web.Response(body=jsonp({"error": "ok"}))

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    socket_family = socket.AF_INET if family == "ipv4" else socket.AF_INET6
    base = await http_server(app, socket_family)
    async with SessionPool((family,)) as pool:
        calls = []

        def provider():
            calls.append(pool.auth[family])
            return pool.auth[family]

        client = SrunClient(family, provider, base_url=base)
        access = AccessContext(family, "01")
        credentials = Credentials(USER, SECRET)
        await client.login(credentials, access)
        await client.logout(USER, access)
        await client.login(credentials, access)
        assert len(calls) == 6
    assert peers == [socket_family] * 6
    assert (
        challenges == [{"username": USER, "ip": "", "double_stack": "1", "callback": CALLBACK}] * 3
    )
    for index, params in enumerate(operations, 1):
        expected = (
            build_logout_params(USER, TOKEN + str(index), ac_id="01")
            if index == 2
            else build_login_params(credentials, TOKEN + str(index), ac_id="01")
        )
        assert params == {**expected, "callback": CALLBACK}
        assert params["ip"] == ""  # client_ip never changes the signed value.


@pytest.mark.asyncio
async def test_pool_isolation_reset_and_provider_refresh(http_server, monkeypatch):
    seen = []

    async def handler(request):
        seen.append(dict(request.headers))
        response = web.Response(text='ip: "192.0.2.1"; ac_id=01')
        response.set_cookie("server-cookie", SECRET)
        return response

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    base = await http_server(app)
    # A dead inherited proxy/netrc configuration must have no effect.
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    pool = SessionPool(("ipv4", "ipv6"))
    auth_dict, traffic_dict = pool.auth, pool.traffic
    async with pool:
        originals = [*pool.auth.values(), *pool.traffic.values()]
        connectors = [s.connector for s in originals]
        for family, socket_family in (("ipv4", socket.AF_INET), ("ipv6", socket.AF_INET6)):
            auth, traffic = pool.auth[family], pool.traffic[family]
            assert auth.connector is not traffic.connector
            assert auth.connector.family == traffic.connector.family == socket_family
            assert auth.connector._ssl is traffic.connector._ssl is True
            assert not auth.trust_env and not traffic.trust_env
            assert isinstance(traffic.cookie_jar, aiohttp.DummyCookieJar)
            assert not traffic.auto_decompress
            assert traffic.timeout.sock_read is None and traffic.timeout.total is None
            assert traffic.timeout.connect == traffic.timeout.sock_connect == 5
            assert auth.timeout.total == auth.timeout.sock_read == 5
            assert traffic._read_bufsize <= 16384
        auth = pool.auth["ipv4"]
        auth.cookie_jar.update_cookies({"auth-cookie": SECRET})
        auth.headers["Authorization"] = "Bearer " + SECRET
        auth.headers["X-Auth-Secret"] = SECRET
        client = client_for(pool, base)
        assert (await client.discover()).ac_id == "01"
        assert "Cookie" not in seen[-1] and "Authorization" not in seen[-1]
        assert "X-Auth-Secret" not in seen[-1]
        async with auth.get(base, allow_redirects=False) as response:
            await response.read()
        assert SECRET in seen[-1]["Cookie"]
        assert SECRET in seen[-1]["Authorization"]
        for _ in range(2):
            async with pool.traffic["ipv4"].get(base, allow_redirects=False) as response:
                assert response.content.get_read_buffer_limits() == (16384, 32768)
                await response.read()
            assert "Cookie" not in seen[-1] and "Authorization" not in seen[-1]
            assert "X-Auth-Secret" not in seen[-1]
        await pool.reset()
        assert pool.auth is auth_dict and pool.traffic is traffic_dict
        assert all(s.closed for s in originals)
        assert all(c.closed for c in connectors)
        assert not pool.auth["ipv4"].cookie_jar
        assert "Authorization" not in pool.auth["ipv4"].headers
        assert (await client.discover()).ac_id == "01"
        replacements = [*pool.auth.values(), *pool.traffic.values()]
    assert pool.auth is auth_dict and pool.traffic is traffic_dict
    assert not auth_dict and not traffic_dict
    assert all(s.closed for s in replacements)
    await pool.close()


@pytest.mark.asyncio
async def test_pool_partial_creation_failure_closes_every_created_session(monkeypatch):
    original = SessionPool._new_session
    created = []

    async def fail(self, family, *, traffic):
        if family == "ipv6":
            raise RuntimeError("synthetic programming failure")
        session = await original(self, family, traffic=traffic)
        created.append(session)
        return session

    monkeypatch.setattr(SessionPool, "_new_session", fail)
    pool = SessionPool(("ipv4", "ipv6"))
    with pytest.raises(RuntimeError):
        await pool.reset()
    assert all(s.closed for s in created)
    assert not pool.auth and not pool.traffic


@pytest.mark.asyncio
async def test_traffic_remains_compressed_and_has_no_background_read_timeout(http_server):
    compressed = gzip.compress(b"synthetic-body" * 100)
    ready, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        response = web.StreamResponse(headers={"Content-Encoding": "gzip"})
        await response.prepare(request)
        ready.set()
        await release.wait()
        await response.write(compressed)
        return response

    app = web.Application()
    app.router.add_get("/", handler)
    base = await http_server(app)
    async with SessionPool(("ipv4",)) as pool, pool.traffic["ipv4"].get(base) as response:
        await ready.wait()
        await asyncio.sleep(0.06)  # Represents a rate-limiter wait, with no read in progress.
        release.set()
        assert await response.content.readexactly(len(compressed)) == compressed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "status", "location", "expected"),
    [
        (b'<a href="/index_001.html">portal</a>', 200, None, "001"),
        (b'<a href="/?a=1&amp;ac_id=12">portal</a>', 200, None, "12"),
        (b"", 302, "/index_13.html", "13"),
        (b"", 307, "http://127.0.0.1:1/?ac_id=14", "14"),
        (b"ac_id=7 ac_id=7", 200, None, "7"),
        (b"ac_id=0", 200, None, None),
        (b"ac_id=-1", 200, None, None),
        (b"ac_id=12345678901234567", 200, None, None),
        (b"ac_id=7secret", 200, None, None),
        (b"not_ac_id=7", 200, None, None),
        (b"ac_id=7 ac_id=8", 200, None, None),
        (b"", 302, "http://127.0.0.1:1/?password=" + SECRET, None),
        (SECRET.encode(), 200, None, None),
        (b"\xff", 200, None, None),
    ],
)
async def test_discovery_html_location_and_no_redirect_following(
    http_server, body, status, location, expected
):
    requests = []

    async def handler(request):
        requests.append(request)
        return web.Response(
            body=body, status=status, headers={"Location": location} if location else {}
        )

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    base = await http_server(app)
    async with SessionPool(("ipv4",)) as pool:
        client = client_for(pool, base)
        if expected is not None:
            assert await client.discover() == AccessContext("ipv4", expected, "")
        else:
            with pytest.raises(ProtocolError) as caught:
                await client.discover()
            assert SECRET not in repr(caught.value)
        assert len(requests) == 1 and requests[0].path == "/discover"
        assert not requests[0].query


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["ipv4", "ipv6"])
async def test_defaults_and_configured_ac_id_take_precedence_without_io(family):
    def forbidden():
        pytest.fail("configured discovery must not obtain a session")

    client = SrunClient(family, forbidden, ac_id="001")
    assert await client.discover() == AccessContext(family, "001")
    assert str(client._base) == f"https://auth{'4' if family == 'ipv4' else '6'}.tsinghua.edu.cn"
    assert str(client._discovery) == (
        "http://login.tsinghua.edu.cn/index_1.html"
        if family == "ipv4"
        else "http://mirrors6.tuna.tsinghua.edu.cn/"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["ipv4", "ipv6"])
@pytest.mark.parametrize(
    ("data", "state"),
    [
        ({"error": "ok", "user_name": USER}, OnlineState.ONLINE),
        ({"error": "not_online"}, OnlineState.OFFLINE),
        ({"error": "not_online", "user_name": ""}, OnlineState.OFFLINE),
        ({"error": "not_online", "user_name": USER}, OnlineState.UNKNOWN),
        ({"error": "ok", "user_name": USER, "ecode": "E2553"}, OnlineState.UNKNOWN),
        ({"error": "not online"}, OnlineState.UNKNOWN),
        ({"error": "E2531"}, OnlineState.UNKNOWN),
        ({"error": SECRET, "user_name": USER}, OnlineState.UNKNOWN),
    ],
)
async def test_status_two_step_flow_and_conservative_states(http_server, family, data, state):
    ip = "192.0.2.1" if family == "ipv4" else "2001:0DB8::1"
    requests = []

    async def handler(request):
        requests.append((request.path, dict(request.query)))
        if request.path == "/srun_portal_pc":
            return web.Response(text=f"var data = {{ip : '{ip}'}};")
        return web.Response(body=jsonp(data))

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    base = await http_server(app, socket.AF_INET if family == "ipv4" else socket.AF_INET6)
    async with SessionPool((family,)) as pool:
        result = await client_for(pool, base, family=family).status(AccessContext(family, "01"))
    assert result.state == state
    assert result.ip == ip
    assert result.username == (USER if state == OnlineState.ONLINE else None)
    assert SECRET not in repr(result) and USER not in repr(result) and ip not in repr(result)
    assert requests == [
        ("/srun_portal_pc", {"ac_id": "01"}),
        ("/cgi-bin/rad_user_info", {"ip": ip, "callback": CALLBACK}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        {},
        {"error": None},
        {"error": ""},
        {"error": "ok"},
        *(
            {"error": "ok", "user_name": name}
            for name in (None, "", " ", 1, "a\nb", "x" * 257, "\ud800", "a\x7fb")
        ),
    ],
)
async def test_malformed_status_never_means_offline(http_server, data):
    async def handler(request):
        return web.Response(
            body=b'ip : "192.0.2.1"' if request.path == "/srun_portal_pc" else jsonp(data)
        )

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    base = await http_server(app)
    async with SessionPool(("ipv4",)) as pool:
        with pytest.raises(ProtocolError):
            await client_for(pool, base).status(ACCESS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "portal",
    [
        b"",
        b'ip: "999.0.0.1"',
        b'client_ip: "192.0.2.1"',
        b'ip: "fe80::1%12"',
        b'ip: "192.0.2.1"; ip: "192.0.2.2"',
        b'ip: "192.0.2.1\n"',
        b"\xff",
    ],
)
async def test_invalid_portal_ip_stops_before_user_info(http_server, portal):
    paths = []

    async def handler(request):
        paths.append(request.path)
        return web.Response(body=portal)

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    base = await http_server(app)
    async with SessionPool(("ipv4",)) as pool:
        with pytest.raises(ProtocolError):
            await client_for(pool, base).status(ACCESS)
    assert paths == ["/srun_portal_pc"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["login", "logout"])
@pytest.mark.parametrize("phase", ["get_challenge", "srun_portal"])
async def test_sensitive_requests_never_follow_redirect_or_replay(http_server, action, phase):
    paths = []
    disconnect = False

    async def handler(request):
        paths.append(request.path)
        if request.path == "/cgi-bin/" + phase:
            if disconnect:
                request.transport.abort()
                return web.Response()
            return web.Response(
                status=307, headers={"Location": "/leak?username=" + USER + "&password=" + SECRET}
            )
        return web.Response(body=jsonp({"res": "ok", "challenge": TOKEN}))

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    base = await http_server(app)
    async with SessionPool(("ipv4",)) as pool:
        client = client_for(pool, base)
        for disconnect in (False, True):
            paths.clear()
            with pytest.raises(NetworkError) as caught:
                if action == "login":
                    await client.login(Credentials(USER, SECRET), ACCESS)
                else:
                    await client.logout(USER, ACCESS)
            assert caught.value.kind == ("connection" if disconnect else "http")
            assert paths.count("/cgi-bin/" + phase) == 1
            assert "/leak" not in paths
            assert len(paths) == (1 if phase == "get_challenge" else 2)
            rendered = "".join(traceback.format_exception(caught.value))
            assert SECRET not in rendered and USER not in rendered and TOKEN not in rendered
            assert base not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 400, 401, 403, 429, 500, 503])
async def test_http_errors_do_not_read_or_disclose_response(http_server, status, caplog):
    ready, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        response = web.StreamResponse(status=status, headers={"Location": "/" + SECRET})
        await response.prepare(request)
        ready.set()
        await release.wait()  # Client must reject headers without waiting for the body.
        return response

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    base = await http_server(app)
    try:
        async with SessionPool(("ipv4",)) as pool:
            with pytest.raises(NetworkError) as caught:
                await client_for(pool, base, timeout_seconds=0.3).login(
                    Credentials(USER, SECRET), ACCESS
                )
            assert ready.is_set()
            assert caught.value.kind == "http"
            assert not pool.auth["ipv4"].connector._acquired
            rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
            assert all(value not in rendered for value in (SECRET, USER, TOKEN, base))
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["get_challenge", "srun_portal"])
@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"<html>" + SECRET.encode(),
        jsonp({"res": "ok", "challenge": TOKEN}, "wrong"),
        b'C_a_l_l_b_a_c_k({"password":"' + SECRET.encode() + b'", "broken":})',
        b'C_a_l_l_b_a_c_k({"res":"ok","res":"ok"})',
        b"\xff",
    ],
)
async def test_malformed_auth_response_is_safe_protocol_error(http_server, phase, body, caplog):
    async def handler(request):
        return web.Response(
            body=body if request.path.endswith(phase) else jsonp({"res": "ok", "challenge": TOKEN})
        )

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    base = await http_server(app)
    async with SessionPool(("ipv4",)) as pool:
        with pytest.raises(ProtocolError) as caught:
            await client_for(pool, base).login(Credentials(USER, SECRET), ACCESS)
    rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert all(value not in rendered for value in (SECRET, USER, TOKEN, base))


@pytest.mark.asyncio
@pytest.mark.parametrize("length_header", [True, False])
async def test_oversize_closes_without_draining_or_unbounded_read(
    http_server, monkeypatch, length_header
):
    release = asyncio.Event()
    transports = []
    read_sizes = []
    original_read = aiohttp.StreamReader.read

    async def bounded_read(self, n=-1):
        read_sizes.append(n)
        assert 0 < n <= 8192
        return await original_read(self, n)

    async def handler(request):
        transports.append(request.transport)
        response = web.StreamResponse(
            headers={"Content-Length": str(MAX_RESPONSE_BYTES * 100)} if length_header else {}
        )
        await response.prepare(request)
        if not length_header:
            await response.write(b"x" * (MAX_RESPONSE_BYTES + 1000))
        await release.wait()  # No EOF until the test releases this handler.
        return response

    app = web.Application()
    app.router.add_get("/discover", handler)
    base = await http_server(app)
    monkeypatch.setattr(aiohttp.StreamReader, "read", bounded_read)
    try:
        async with SessionPool(("ipv4",)) as pool:
            with pytest.raises(ProtocolError) as caught:
                await client_for(pool, base, timeout_seconds=0.5).discover()
            assert caught.value.code == "response_too_large"
            assert not pool.auth["ipv4"].connector._acquired
            async with asyncio.timeout(1):
                while not transports[0].is_closing():
                    await asyncio.sleep(0.005)
        assert not read_sizes if length_header else bool(read_sizes)
    finally:
        release.set()


@pytest.mark.asyncio
async def test_exact_size_limit_is_accepted_and_compressed_auth_is_rejected(http_server):
    body = b"ac_id=01 " + b" " * (MAX_RESPONSE_BYTES - len(b"ac_id=01 "))
    compressed = False

    async def handler(request):
        return web.Response(
            body=gzip.compress(body) if compressed else body,
            headers={"Content-Encoding": "gzip"} if compressed else {},
        )

    app = web.Application()
    app.router.add_get("/discover", handler)
    base = await http_server(app)
    async with SessionPool(("ipv4",)) as pool:
        client = client_for(pool, base)
        assert (await client.discover()).ac_id == "01"
        compressed = True
        with pytest.raises(ProtocolError):
            await client.discover()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["headers", "body", "dribble"])
async def test_header_body_and_total_timeouts_are_safe_and_release_connections(http_server, phase):
    release = asyncio.Event()
    task_finished = asyncio.Event()

    async def handler(request):
        try:
            if phase == "headers":
                await release.wait()
                return web.Response()
            response = web.StreamResponse()
            await response.prepare(request)
            if phase == "body":
                await response.write(b"a")
                await release.wait()
            else:
                while not release.is_set():
                    await response.write(b"a")
                    await asyncio.sleep(0.02)
            return response
        except (ConnectionResetError, aiohttp.ClientConnectionError):
            return web.Response()
        finally:
            task_finished.set()

    app = web.Application()
    app.router.add_get("/discover", handler)
    base = await http_server(app)
    try:
        async with SessionPool(("ipv4",)) as pool:
            with pytest.raises(NetworkError) as caught:
                await client_for(pool, base, timeout_seconds=0.12).discover()
            assert caught.value.kind == "timeout"
            assert not pool.auth["ipv4"].connector._acquired
    finally:
        release.set()
        await asyncio.wait_for(task_finished.wait(), 1)


@pytest.mark.asyncio
async def test_truncated_response_is_connection_error(http_server):
    async def handler(request):
        response = web.StreamResponse(headers={"Content-Length": "1000"})
        await response.prepare(request)
        await response.write(SECRET.encode())
        request.transport.abort()
        return response

    app = web.Application()
    app.router.add_get("/discover", handler)
    base = await http_server(app)
    async with SessionPool(("ipv4",)) as pool:
        with pytest.raises(NetworkError) as caught:
            await client_for(pool, base).discover()
        assert caught.value.kind == "connection"
        assert SECRET not in "".join(traceback.format_exception(caught.value))
        assert not pool.auth["ipv4"].connector._acquired


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["discover", "status", "login", "logout", "diagnose"])
@pytest.mark.parametrize("phase", ["headers", "body"])
async def test_cancellation_propagates_for_every_method_and_closes_responses(
    http_server, method, phase
):
    ready, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        response = web.StreamResponse()
        if phase == "body":
            await response.prepare(request)
            await response.write(b"a")
        ready.set()
        await release.wait()
        return response

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    base = await http_server(app)
    pool = SessionPool(("ipv4",))
    try:
        async with pool:
            session = pool.auth["ipv4"]
            client = client_for(pool, base)
            arguments = {
                "discover": (),
                "status": (ACCESS,),
                "login": (Credentials(USER, SECRET), ACCESS),
                "logout": (USER, ACCESS),
                "diagnose": (),
            }
            task = asyncio.create_task(getattr(client, method)(*arguments[method]))
            await asyncio.wait_for(ready.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not session.connector._acquired
            assert not session.closed
        assert session.closed
    finally:
        release.set()


def connector_error(error, *, dns=False):
    key = ConnectionKey(SECRET, 443, True, True, None, None, None)
    cls = aiohttp.ClientConnectorDNSError if dns else aiohttp.ClientConnectorError
    return cls(key, error)


def windows_error(code):
    error = OSError(None, SECRET)
    error.winerror = code
    return error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (TimeoutError(SECRET), "timeout"),
        (connector_error(TimeoutError(SECRET)), "timeout"),
        (connector_error(OSError(errno.ETIMEDOUT, SECRET)), "timeout"),
        (connector_error(windows_error(10060)), "timeout"),
        (ssl.SSLCertVerificationError(1, SECRET), "tls"),
        (aiohttp.ClientPayloadError(SECRET), "connection"),
        (aiohttp.ClientResponseError(None, (), status=500, message=SECRET), "http"),
        (connector_error(socket.gaierror(socket.EAI_NONAME, SECRET)), "dns"),
        (connector_error(OSError(errno.ENETUNREACH, SECRET), dns=True), "dns"),
        (socket.gaierror(socket.EAI_NONAME, SECRET), "dns"),
        (connector_error(OSError(errno.ECONNREFUSED, SECRET)), "connection"),
        *(
            (connector_error(OSError(code, SECRET)), "unavailable")
            for code in (
                errno.EAFNOSUPPORT,
                errno.EADDRNOTAVAIL,
                errno.ENETDOWN,
                errno.ENETUNREACH,
                errno.EHOSTUNREACH,
                10047,
                10049,
                10050,
                10051,
                10065,
            )
        ),
        *(
            (connector_error(windows_error(code)), "unavailable")
            for code in (10047, 10049, 10051, 10065)
        ),
    ],
    ids=lambda item: item if isinstance(item, str) else type(item).__name__,
)
async def test_network_classification_sanitizes_underlying_errors(monkeypatch, error, kind, caplog):
    async def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(SrunClient, "_send", fail)

    async def resolved(*args, **kwargs):
        return [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::1", 80, 0, 0))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolved)
    async with SessionPool(("ipv6",)) as pool:
        client = client_for(pool, "http://[::1]", family="ipv6", ac_id="01")
        with pytest.raises(NetworkError) as caught:
            await client.status(AccessContext("ipv6", "01"))
        assert caught.value.kind == kind
        assert caught.value.__cause__ is None and caught.value.__suppress_context__
        rendered = (
            "".join(traceback.format_exception(caught.value)) + repr(caught.value) + caplog.text
        )
        assert SECRET not in rendered
        assert "::1" not in rendered
        results = await client.diagnose()
        assert results["portal"] == kind
        if kind == "dns":
            assert results["dns"] == "ok" and "unavailable" not in results.values()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["discover", "status", "login", "logout", "diagnose"])
async def test_unknown_programming_exceptions_are_not_swallowed(monkeypatch, method):
    async def fail(*args, **kwargs):
        raise RuntimeError("synthetic coding bug")

    monkeypatch.setattr(SrunClient, "_send", fail)
    async with SessionPool(("ipv4",)) as pool:
        client = client_for(pool, "http://127.0.0.1")
        arguments = {
            "discover": (),
            "status": (ACCESS,),
            "login": (Credentials(USER, SECRET), ACCESS),
            "logout": (USER, ACCESS),
            "diagnose": (),
        }
        with pytest.raises(RuntimeError, match="synthetic coding bug"):
            await getattr(client, method)(*arguments[method])


@pytest.mark.asyncio
async def test_diagnose_is_anonymous_read_only_and_uses_only_fixed_categories(http_server):
    requests = []

    async def handler(request):
        requests.append((request.path, dict(request.query), dict(request.headers)))
        return web.Response(text="ac_id=01 " + SECRET)

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    base = await http_server(app)
    async with SessionPool(("ipv4",)) as pool:
        pool.auth["ipv4"].headers["Authorization"] = "Bearer " + SECRET
        pool.auth["ipv4"].cookie_jar.update_cookies({"auth": SECRET})
        result = await client_for(pool, base).diagnose()
        assert result == {"dns": "ok", "portal": "ok", "tls": "unknown", "ac_id": "ok"}
        assert [p for p, _, _ in requests] == ["/discover", "/srun_portal_pc"]
        assert requests[0][1] == {} and requests[1][1] == {"ac_id": "01"}
        assert all(
            "Cookie" not in headers and "Authorization" not in headers for _, _, headers in requests
        )
        assert SECRET not in repr(result)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"family": "both"},
        {"ac_id": "0"},
        {"ac_id": SECRET},
        {"ac_id": 1},
        {"timeout_seconds": 0},
        {"timeout_seconds": -1},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": True},
        {"base_url": "http://" + USER + ":" + SECRET + "@localhost"},
        {"base_url": "http://localhost/?password=" + SECRET},
        {"base_url": "http://localhost/" + SECRET},
        {"base_url": "ftp://localhost"},
        {"discovery_url": "http://localhost/#" + SECRET},
        {"base_url": "http://localhost:99999"},
    ],
)
def test_invalid_configuration_is_sanitized_without_network(kwargs):
    values = {"family": "ipv4", "session_provider": lambda: pytest.fail("unexpected I/O"), **kwargs}
    with pytest.raises(ProtocolError) as caught:
        SrunClient(**values)
    assert SECRET not in repr(caught.value) and caught.value.__suppress_context__


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "access",
    [AccessContext("ipv6", "01"), AccessContext("ipv4", "0"), AccessContext("ipv4", "01", SECRET)],
)
async def test_invalid_access_never_sends_credentials(access):
    client = SrunClient("ipv4", lambda: pytest.fail("invalid access must not send a request"))
    with pytest.raises(ProtocolError):
        await client.login(Credentials(USER, SECRET), access)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["closed", "unspecified_family", "wrong_family", "proxy_environment"]
)
async def test_invalid_provider_cannot_silently_create_or_use_another_stack(mode):
    family = socket.AF_UNSPEC if mode == "unspecified_family" else socket.AF_INET
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(family=family), trust_env=mode == "proxy_environment"
    ) as session:
        if mode == "closed":
            await session.close()
        client = SrunClient("ipv6" if mode == "wrong_family" else "ipv4", lambda: session)
        with pytest.raises(RuntimeError if mode == "closed" else ProtocolError):
            await client.discover()


@pytest.mark.asyncio
async def test_wrong_literal_target_does_not_fallback_or_claim_stack_unavailable():
    async with SessionPool(("ipv6",)) as pool:
        client = client_for(pool, "http://127.0.0.1:1", family="ipv6")
        with pytest.raises(NetworkError) as caught:
            await client.discover()
        assert caught.value.kind == "connection"


@pytest.mark.asyncio
async def test_cancelling_pool_close_waits_for_all_cleanup_then_propagates(monkeypatch):
    original = aiohttp.ClientSession.close
    started, release = asyncio.Event(), asyncio.Event()
    closed = []

    async def delayed_close(self):
        started.set()
        await release.wait()
        await original(self)
        closed.append(self)

    pool = SessionPool(("ipv4", "ipv6"))
    await pool.reset()
    sessions = [*pool.auth.values(), *pool.traffic.values()]
    monkeypatch.setattr(aiohttp.ClientSession, "close", delayed_close)
    task = asyncio.create_task(pool.close())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert set(closed) == set(sessions) and all(session.closed for session in sessions)


@pytest.mark.asyncio
async def test_tls_verification_cannot_inherit_disabled_connector_settings(
    http_server, monkeypatch
):
    async def handler(request):
        return web.Response(text="ac_id=01")

    app = web.Application()
    app.router.add_get("/discover", handler)
    base = await http_server(app)
    original = aiohttp.ClientSession.get
    contexts = []

    def inspect(self, *args, **kwargs):
        contexts.append(kwargs["ssl"])
        assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["ssl"].check_hostname
        return original(self, *args, **kwargs)

    monkeypatch.setattr(aiohttp.ClientSession, "get", inspect)
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(family=socket.AF_INET, ssl=False), trust_env=False
    ) as session:
        client = SrunClient("ipv4", lambda: session, discovery_url=base + "/discover")
        assert (await client.discover()).ac_id == "01"
    assert len(contexts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "kind"),
    [
        ({"error": "ok"}, AuthReplyKind.ACCEPTED),
        ({"error": SECRET, "ecode": "E2553"}, AuthReplyKind.BLOCKED),
        ({"error": SECRET, "ecode": "E2532"}, AuthReplyKind.THROTTLED),
        ({"error": SECRET, "ecode": "E2620"}, AuthReplyKind.ALREADY_ONLINE),
        ({"error": SECRET, "ecode": SECRET}, AuthReplyKind.REJECTED),
    ],
)
async def test_auth_reply_classification_never_exposes_remote_material(
    http_server, data, kind, caplog
):
    async def handler(request):
        return web.Response(
            body=jsonp(
                {"res": "ok", "challenge": TOKEN}
                if request.path.endswith("get_challenge")
                else data
            )
        )

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    base = await http_server(app)
    async with SessionPool(("ipv4",)) as pool:
        result = await client_for(pool, base).login(Credentials(USER, SECRET), ACCESS)
        assert result.kind == kind
        assert all(value not in repr(result) + caplog.text for value in (SECRET, TOKEN, USER))


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["ipv4", "ipv6"])
@pytest.mark.parametrize("ac_id", [None, "001"])
async def test_status_without_access_does_not_depend_on_discovery(http_server, family, ac_id):
    requests = []
    ip = "192.0.2.1" if family == "ipv4" else "2001:db8::1"

    async def handler(request):
        requests.append((request.path, dict(request.query)))
        if request.path == "/discover":
            return web.Response(text="normal mirror index; no access information")
        if request.path == "/srun_portal_pc":
            return web.Response(text=f'ip: "{ip}"')
        if request.path == "/cgi-bin/rad_user_info":
            return web.Response(body=jsonp({"error": "ok", "user_name": USER}))
        pytest.fail("read-only status attempted authentication")

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    base = await http_server(app, socket.AF_INET if family == "ipv4" else socket.AF_INET6)
    async with SessionPool((family,)) as pool:
        client = client_for(pool, base, family=family, ac_id=ac_id)
        if ac_id is None:
            with pytest.raises(ProtocolError):
                await client.discover()
            requests.clear()
        result = await client.status()
        assert result.state == OnlineState.ONLINE and result.username == USER
        assert requests == [
            ("/srun_portal_pc", {"ac_id": ac_id or ""}),
            ("/cgi-bin/rad_user_info", {"ip": ip, "callback": CALLBACK}),
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["ipv4", "ipv6"])
async def test_diagnose_performs_independent_explicit_family_getaddrinfo(
    http_server, monkeypatch, family
):
    async def handler(request):
        return web.Response(text="normal portal")

    app = web.Application()
    app.router.add_get("/srun_portal_pc", handler)
    socket_family = socket.AF_INET if family == "ipv4" else socket.AF_INET6
    base = await http_server(app, socket_family)
    loop = asyncio.get_running_loop()
    original = loop.getaddrinfo
    lookups = []

    async def observed(*args, **kwargs):
        lookups.append((args, kwargs))
        return await original(*args, **kwargs)

    monkeypatch.setattr(loop, "getaddrinfo", observed)
    async with SessionPool((family,)) as pool:
        client = client_for(pool, base, family=family, ac_id="01")
        result = await client.diagnose()
        assert result == {"dns": "ok", "portal": "ok", "tls": "unknown", "ac_id": "configured"}
        assert lookups == [
            (
                (client._base.host, client._base.port),
                {"family": socket_family, "type": socket.SOCK_STREAM, "proto": socket.IPPROTO_TCP},
            )
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (socket.gaierror(socket.EAI_NONAME, SECRET), "failed"),
        (socket.gaierror(errno.ENETUNREACH, SECRET), "failed"),
        (TimeoutError(SECRET), "timeout"),
        (OSError(errno.EAFNOSUPPORT, SECRET), "unavailable"),
        (windows_error(10051), "unavailable"),
        (OSError(errno.ECONNREFUSED, SECRET), "failed"),
        ([], "failed"),
        ([(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 80, 0, 0))], "failed"),
    ],
    ids=lambda item: item if isinstance(item, str) else type(item).__name__,
)
async def test_diagnose_dns_results_are_local_safe_and_independent(
    http_server, monkeypatch, failure, expected, caplog
):
    async def handler(request):
        return web.Response(text="ac_id=01")

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    base = await http_server(app)

    async def lookup(*args, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
    async with SessionPool(("ipv4",)) as pool:
        result = await client_for(pool, base).diagnose()
    assert result == {"dns": expected, "portal": "ok", "tls": "unknown", "ac_id": "ok"}
    assert SECRET not in repr(result) + caplog.text and base not in repr(result)


@pytest.mark.asyncio
async def test_diagnose_dns_await_has_timeout_and_checks_continue(http_server, monkeypatch):
    cancelled = asyncio.Event()

    async def lookup(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def handler(request):
        return web.Response(text="ac_id=01")

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    base = await http_server(app)
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
    async with SessionPool(("ipv4",)) as pool, asyncio.timeout(1):
        result = await client_for(pool, base, timeout_seconds=0.05).diagnose()
    assert cancelled.is_set()
    assert result == {"dns": "timeout", "portal": "ok", "tls": "unknown", "ac_id": "ok"}


@pytest.mark.asyncio
async def test_diagnose_dns_cancellation_and_programming_errors_propagate(monkeypatch):
    started = asyncio.Event()

    async def lookup(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", lookup)
    client = SrunClient(
        "ipv4",
        lambda: pytest.fail("DNS cancellation must stop HTTP requests"),
        base_url="http://127.0.0.1",
    )
    task = asyncio.create_task(client.diagnose())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async def broken(*args, **kwargs):
        raise RuntimeError("synthetic resolver bug")

    monkeypatch.setattr(loop, "getaddrinfo", broken)
    with pytest.raises(RuntimeError, match="synthetic resolver bug"):
        await client.diagnose()
