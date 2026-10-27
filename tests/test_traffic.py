from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import format_datetime
from time import monotonic
from urllib.parse import urlsplit

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from thuauth.config import AuthConfig, BulkConfig, Config, ProbeConfig
from thuauth.models import Target
from thuauth.traffic import TrafficRunner


@pytest_asyncio.fixture
async def sessions():
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(family=socket.AF_INET),
        cookie_jar=aiohttp.DummyCookieJar(),
        auto_decompress=False,
        trust_env=False,
    ) as session:
        yield {"ipv4": session}


def configuration(url: str, **bulk_options) -> Config:
    bulk = BulkConfig(
        enabled=True,
        workers_ipv4=1,
        workers_ipv6=0,
        chunk_bytes=128,
        max_response_bytes=512,
        targets=(Target(url),),
    )
    return Config(
        auth=AuthConfig(ipv6=False),
        probe=ProbeConfig(targets=()),
        bulk=replace(bulk, **bulk_options),
    )


async def fetch_once(runner: TrafficRunner, family="ipv4", index=0, *, bulk=True) -> None:
    targets = runner._config.bulk.targets if bulk else runner._config.probe.targets
    target = targets[index]
    await runner._fetch(family, target, runner._states[target.url], bulk=bulk)


async def until(predicate, timeout=3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def cancel(task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 206])
async def test_real_finite_download_range_and_ignored_range(http_server, sessions, status):
    async def handler(request):
        assert request.headers["Range"] == "bytes=0-511"
        assert request.headers["Accept-Encoding"] == "identity"
        assert "Cookie" not in request.headers
        headers = {"Content-Range": "bytes 0-511/4096"} if status == 206 else {}
        return web.Response(
            body=b"x" * (512 if status == 206 else 4096), status=status, headers=headers
        )

    app = web.Application()
    app.router.add_get("/file", handler)
    url = await http_server(app) + "/file?secret=SYNTHETIC_QUERY_SECRET"
    runner = TrafficRunner(configuration(url), sessions, lambda _: True)
    await fetch_once(runner)
    stats = runner.snapshot()["bulk"]["ipv4"]
    assert stats["bytes"] == 512
    assert stats["successes"] == 1
    assert stats["failures"] == 0
    assert not sessions["ipv4"].connector._acquired
    assert "SYNTHETIC_QUERY_SECRET" not in json.dumps(runner.snapshot())
    assert url not in repr(runner)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_range",
    [
        None,
        "bytes 1-512/4096",
        "bytes 0-512/4096",
        "bytes 0-511/511",
        "bytes 0-511/*",
        "bytes 5-2/10",
        "bytes 0-510/4096",
    ],
)
async def test_invalid_206_metadata_is_rejected(http_server, sessions, content_range):
    async def handler(request):
        headers = {"Content-Range": content_range} if content_range else {}
        return web.Response(body=b"x" * 512, status=206, headers=headers)

    app = web.Application()
    app.router.add_get("/", handler)
    runner = TrafficRunner(configuration(await http_server(app)), sessions, lambda _: True)
    await fetch_once(runner)
    assert runner.snapshot()["bulk"]["ipv4"]["last_result"] == "invalid_range"
    assert runner.snapshot()["bulk"]["ipv4"]["bytes"] == 0


@pytest.mark.asyncio
async def test_short_valid_range_and_truncated_chunked_range(http_server, sessions):
    async def handler(request):
        response = web.StreamResponse(status=206, headers={"Content-Range": "bytes 0-3/4"})
        await response.prepare(request)
        await response.write(b"abc" if request.path == "/short" else b"abcd")
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/{mode}", handler)
    base = await http_server(app)
    for path, result in (("/ok", "ok"), ("/short", "truncated_range")):
        runner = TrafficRunner(configuration(base + path), sessions, lambda _: True)
        await fetch_once(runner)
        assert runner.snapshot()["bulk"]["ipv4"]["last_result"] == result


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["redirect", "html", "sniff", "empty", "compressed"])
async def test_non_bulk_payloads_are_not_progress(http_server, sessions, caplog, mode):
    login_hits = 0

    async def handler(request):
        if mode == "redirect":
            return web.Response(status=302, headers={"Location": "/login?token=SYNTHETIC_SECRET"})
        if mode == "html":
            return web.Response(text="login", content_type="text/html")
        if mode == "empty":
            return web.Response(body=b"")
        if mode == "compressed":
            return web.Response(body=b"invalid-gzip", headers={"Content-Encoding": "gzip"})
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b"<ht")
        await asyncio.sleep(0.02)
        await response.write(b"ml>login</html>")
        await response.write_eof()
        return response

    async def login(request):
        nonlocal login_hits
        login_hits += 1
        return web.Response(body=b"not a bulk target")

    app = web.Application()
    app.router.add_get("/", handler)
    app.router.add_get("/login", login)
    runner = TrafficRunner(configuration(await http_server(app)), sessions, lambda _: True)
    await fetch_once(runner)
    stats = runner.snapshot()["bulk"]["ipv4"]
    assert stats["successes"] == 0
    assert stats["failures"] == 1
    assert login_hits == 0
    assert "SYNTHETIC_SECRET" not in caplog.text + json.dumps(runner.snapshot())
    assert not sessions["ipv4"].connector._acquired


@pytest.mark.asyncio
async def test_short_reads_refund_unused_budget(http_server, sessions):
    async def handler(request):
        return web.Response(body=b"abc")

    app = web.Application()
    app.router.add_get("/", handler)
    runner = TrafficRunner(
        configuration(await http_server(app)), sessions, lambda _: True, clock=lambda: 0.0
    )
    await fetch_once(runner)
    assert runner.snapshot()["bulk"]["ipv4"]["bytes"] == 3
    assert runner.snapshot()["bulk"]["ipv4"]["successes"] == 1
    assert runner._limiter._tokens == runner._limiter.burst_bytes - 3


@pytest.mark.asyncio
async def test_actual_slow_stream_succeeds_without_buffering_whole_body(http_server, sessions):
    async def handler(request):
        response = web.StreamResponse()
        await response.prepare(request)
        for _ in range(8):
            await response.write(b"x" * 64)
            await asyncio.sleep(0.02)
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/", handler)
    runner = TrafficRunner(
        configuration(await http_server(app)), sessions, lambda _: True, read_timeout=0.15
    )
    await fetch_once(runner)
    assert runner.snapshot()["bulk"]["ipv4"]["successes"] == 1
    assert runner.snapshot()["bulk"]["ipv4"]["bytes"] == 512


@pytest.mark.asyncio
async def test_limiter_wait_is_not_read_timeout_and_total_scales_with_quota(http_server, sessions):
    async def handler(request):
        return web.Response(body=b"x" * 384)

    app = web.Application()
    app.router.add_get("/", handler)
    runner = TrafficRunner(
        configuration(
            await http_server(app),
            download_mib_per_minute=128 * 60 / 1024**2,
            max_response_bytes=384,
        ),
        sessions,
        lambda _: True,
        read_timeout=0.1,
        connect_timeout=0.1,
    )
    started = monotonic()
    await fetch_once(runner)
    assert monotonic() - started >= 1.9
    assert runner.snapshot()["bulk"]["ipv4"]["successes"] == 1


@pytest.mark.asyncio
async def test_read_stall_times_out_closes_response_and_refunds(http_server, sessions):
    release = asyncio.Event()

    async def handler(request):
        response = web.StreamResponse()
        await response.prepare(request)
        await release.wait()
        return response

    app = web.Application()
    app.router.add_get("/", handler)
    runner = TrafficRunner(
        configuration(await http_server(app)), sessions, lambda _: True, read_timeout=0.05
    )
    try:
        await fetch_once(runner)
        stats = runner.snapshot()["bulk"]["ipv4"]
        assert stats["last_result"] == "network_error"
        assert stats["bytes"] == 0
        assert runner._limiter._tokens == runner._limiter.burst_bytes
        assert not sessions["ipv4"].connector._acquired
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["read", "budget"])
async def test_cancel_active_request_releases_response_and_reservations(
    http_server, sessions, phase
):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(request):
        response = web.StreamResponse()
        await response.prepare(request)
        entered.set()
        await release.wait()
        return response

    app = web.Application()
    app.router.add_get("/", handler)
    runner = TrafficRunner(configuration(await http_server(app)), sessions, lambda _: True)
    if phase == "budget":
        runner._limiter.rate = 0.001
        await runner._limiter.acquire(runner._limiter.burst_bytes)
    task = asyncio.create_task(fetch_once(runner))
    try:
        await entered.wait()
        await asyncio.sleep(0.02)
        await cancel(task)
        assert not runner._limiter._queue
        assert not sessions["ipv4"].connector._acquired
        if phase == "read":
            assert runner._limiter._tokens == runner._limiter.burst_bytes
    finally:
        release.set()
        if not task.done():
            await cancel(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 429, 503])
async def test_all_failed_sources_cool_down_instead_of_busy_looping(http_server, sessions, status):
    hits = 0

    async def handler(request):
        nonlocal hits
        hits += 1
        return web.Response(status=status, headers={"Retry-After": "60"})

    app = web.Application()
    app.router.add_get("/", handler)
    runner = TrafficRunner(
        configuration(await http_server(app), workers_ipv4=4),
        sessions,
        lambda _: True,
        request_interval=0.02,
    )
    task = asyncio.create_task(runner.run())
    try:
        await until(lambda: runner.snapshot()["bulk"]["ipv4"]["failures"] == 1)
        await asyncio.sleep(0.15)
        assert hits == 1
        assert runner.snapshot()["targets"]["bulk"][0]["cooldown_seconds"] > 4
    finally:
        await cancel(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after", ["120", "date", "999999999999999999999999999999", "99999"])
async def test_429_shared_across_stacks_workers_and_reset(http_server, sessions, retry_after):
    hits = 0
    wall = 1700000000.0
    header = (
        format_datetime(datetime.fromtimestamp(wall + 120, UTC), usegmt=True)
        if retry_after == "date"
        else retry_after
    )

    async def handler(request):
        nonlocal hits
        hits += 1
        return web.Response(status=429, headers={"Retry-After": header})

    app = web.Application()
    app.router.add_get("/", handler)
    url = await http_server(app)
    config = configuration(
        url, workers_ipv4=3, workers_ipv6=3, targets=(Target(url, "ipv4"), Target(url, "ipv6"))
    )
    config = replace(config, auth=AuthConfig())
    # Two logical families exercise shared scheduling; genuine IPv6 is tested below.
    mapping = {"ipv4": sessions["ipv4"], "ipv6": sessions["ipv4"]}
    runner = TrafficRunner(
        config,
        mapping,
        lambda _: True,
        clock=lambda: 100.0,
        wall_clock=lambda: wall,
        request_interval=0.02,
    )
    task = asyncio.create_task(runner.run())
    try:
        await until(lambda: hits > 0 and runner.snapshot()["bulk"]["ipv4"]["failures"] > 0)
        runner.reset()
        await asyncio.sleep(0.1)
        assert hits == 1
        targets = runner.snapshot()["targets"]["bulk"]
        assert targets[0]["failures"] == targets[1]["failures"] == 1
        if retry_after in ("120", "date"):
            assert targets[0]["cooldown_seconds"] == targets[1]["cooldown_seconds"] == 120
        else:
            assert targets[0]["paused"] and targets[1]["paused"]
    finally:
        await cancel(task)


@pytest.mark.asyncio
async def test_concurrent_success_does_not_clear_newer_429(http_server, sessions):
    started = asyncio.Event()
    release = asyncio.Event()
    hits = 0

    async def handler(request):
        nonlocal hits
        hits += 1
        if hits == 1:
            started.set()
            await release.wait()
            return web.Response(body=b"abc")
        return web.Response(status=429, headers={"Retry-After": "600"})

    app = web.Application()
    app.router.add_get("/", handler)
    runner = TrafficRunner(configuration(await http_server(app)), sessions, lambda _: True)
    first = asyncio.create_task(fetch_once(runner))
    try:
        await started.wait()
        await fetch_once(runner)
        release.set()
        await first
        assert runner.snapshot()["targets"]["bulk"][0]["cooldown_seconds"] > 590
        assert runner.snapshot()["targets"]["bulk"][0]["failures"] == 1
    finally:
        release.set()
        if not first.done():
            await cancel(first)


@pytest.mark.asyncio
async def test_unknown_pauses_bulk_but_probe_rotates_one_target_per_tick(http_server, sessions):
    paths = []

    async def handler(request):
        paths.append(request.path)
        return web.Response(body=b"p" * 4096)

    app = web.Application()
    app.router.add_get("/{path}", handler)
    base = await http_server(app)
    config = replace(
        configuration(base + "/bulk", workers_ipv4=4),
        probe=ProbeConfig(
            interval_seconds=0.08,
            max_response_bytes=32,
            targets=(Target(base + "/one"), Target(base + "/two")),
        ),
    )
    runner = TrafficRunner(config, sessions, lambda _: False, request_interval=0.01)
    task = asyncio.create_task(runner.run())
    try:
        await until(lambda: runner.snapshot()["probe"]["ipv4"]["successes"] == 2)
        assert paths == ["/one", "/two"]
        assert runner.snapshot()["bulk"]["ipv4"]["requests"] == 0
        assert runner.snapshot()["probe"]["ipv4"]["bytes"] == 64
    finally:
        await cancel(task)


@pytest.mark.asyncio
async def test_probe_both_visits_each_stack_and_does_not_follow_redirect(http_server, sessions):
    calls = []

    async def handler(request):
        calls.append(request.path)
        return web.Response(body=b"probe")

    async def redirect(request):
        return web.Response(status=302, headers={"Location": "/followed"})

    app = web.Application()
    app.router.add_get("/", handler)
    app.router.add_get("/followed", handler)
    app.router.add_get("/redirect", redirect)
    # Logical-family scheduling test; genuine IPv6 transport is covered separately.
    base = (await http_server(app)).replace("127.0.0.1", "localhost")
    config = replace(
        configuration(base),
        auth=AuthConfig(),
        bulk=BulkConfig(),
        probe=ProbeConfig(
            interval_seconds=0.03, targets=(Target(base, "both"), Target(base + "/redirect"))
        ),
    )
    runner = TrafficRunner(
        config, {"ipv4": sessions["ipv4"], "ipv6": sessions["ipv4"]}, lambda _: False
    )
    task = asyncio.create_task(runner.run())
    try:
        await until(lambda: runner.snapshot()["probe"]["ipv4"]["redirects"] == 1)
        assert calls == ["/", "/"]
        assert runner.snapshot()["probe"]["ipv6"]["successes"] == 1
        assert runner.snapshot()["bulk"]["ipv4"]["requests"] == 0
    finally:
        await cancel(task)


@pytest.mark.asyncio
async def test_genuine_ipv6_loopback_download(http_server):
    async def handler(request):
        return web.Response(body=b"v6-data")

    app = web.Application()
    app.router.add_get("/", handler)
    url = await http_server(app, family=socket.AF_INET6)
    config = replace(
        configuration(url, workers_ipv4=0, workers_ipv6=1, targets=(Target(url, "ipv6"),)),
        auth=AuthConfig(ipv4=False),
    )
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(family=socket.AF_INET6),
        cookie_jar=aiohttp.DummyCookieJar(),
        trust_env=False,
        auto_decompress=False,
    ) as session:
        runner = TrafficRunner(config, {"ipv6": session}, lambda _: True)
        await fetch_once(runner, "ipv6")
        assert runner.snapshot()["bulk"]["ipv6"]["bytes"] == 7
        assert runner.snapshot()["bulk"]["ipv6"]["successes"] == 1


@pytest.mark.asyncio
async def test_session_is_looked_up_again_after_pool_reset(http_server, sessions):
    async def handler(request):
        return web.Response(body=b"abc")

    app = web.Application()
    app.router.add_get("/", handler)
    runner = TrafficRunner(configuration(await http_server(app)), sessions, lambda _: True)
    await fetch_once(runner)
    await sessions["ipv4"].close()
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(family=socket.AF_INET),
        cookie_jar=aiohttp.DummyCookieJar(),
        trust_env=False,
    ) as replacement:
        sessions["ipv4"] = replacement
        runner.reset()
        await fetch_once(runner)
    assert runner.snapshot()["bulk"]["ipv4"]["successes"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,with_targets", [(False, True), (True, False), (False, False)])
async def test_no_work_stays_alive_until_cancelled(sessions, enabled, with_targets):
    config = configuration("http://127.0.0.1:1/", enabled=enabled)
    if not with_targets:
        config = replace(config, bulk=replace(config.bulk, targets=()))
    runner = TrafficRunner(config, sessions, lambda _: True)
    before = asyncio.all_tasks()
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.03)
    assert not task.done()
    assert runner.snapshot()["bulk"]["ipv4"]["requests"] == 0
    await cancel(task)
    assert asyncio.all_tasks() == before


@pytest.mark.asyncio
async def test_unknown_programming_error_escapes_task_group(sessions):
    def broken_predicate(family):
        raise RuntimeError("synthetic predicate defect")

    runner = TrafficRunner(configuration("http://127.0.0.1:1/"), sessions, broken_predicate)
    with pytest.raises(ExceptionGroup) as captured:
        await runner.run()
    assert any(isinstance(error, RuntimeError) for error in captured.value.exceptions)


@pytest.mark.asyncio
@pytest.mark.parametrize("workers", [1, 4])
async def test_real_multiple_workers_obey_one_long_term_budget(http_server, sessions, workers):
    hits = []

    async def handler(request):
        hits.append(request.path)
        return web.Response(body=b"x" * 65536)

    app = web.Application()
    app.router.add_get("/{source}", handler)
    base = await http_server(app)
    rate = 4096
    config = configuration(
        base + "/0",
        workers_ipv4=workers,
        download_mib_per_minute=rate * 60 / 1024**2,
        chunk_bytes=512,
        max_response_bytes=65536,
        targets=tuple(Target(base + f"/{i}") for i in range(workers)),
    )
    runner = TrafficRunner(config, sessions, lambda _: True, request_interval=0.01)
    before = asyncio.all_tasks()
    started = monotonic()
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(2.0)
    await cancel(task)
    elapsed = monotonic() - started
    read = runner.snapshot()["bulk"]["ipv4"]["bytes"]
    burst = runner.snapshot()["burst_bytes"]
    assert read <= rate * elapsed + burst + 1
    assert read >= rate * elapsed * 0.65
    assert len(set(hits)) == workers
    assert not runner._limiter._queue
    assert not sessions["ipv4"].connector._acquired
    # aiohttp server keepalive cleanup may still be queued; no traffic tasks survive.
    assert not [t for t in asyncio.all_tasks() - before if t.get_name().startswith("traffic-")]


@pytest.mark.asyncio
async def test_low_quota_does_not_extend_header_wait(http_server, sessions):
    release = asyncio.Event()

    async def handler(request):
        await release.wait()
        return web.Response(body=b"abc")

    app = web.Application()
    app.router.add_get("/", handler)
    runner = TrafficRunner(
        configuration(await http_server(app), download_mib_per_minute=0.01),
        sessions,
        lambda _: True,
        connect_timeout=0.05,
        read_timeout=0.05,
    )
    try:
        async with asyncio.timeout(1):
            await fetch_once(runner)
        assert runner.snapshot()["bulk"]["ipv4"]["last_result"] == "network_error"
        assert not sessions["ipv4"].connector._acquired
    finally:
        release.set()


@pytest.mark.asyncio
async def test_two_real_stacks_share_budget_without_starvation(http_server, sessions):
    async def handler(request):
        return web.Response(body=b"x" * 65536)

    app4 = web.Application()
    app4.router.add_get("/", handler)
    app6 = web.Application()
    app6.router.add_get("/", handler)
    url4 = await http_server(app4)
    url6 = await http_server(app6, family=socket.AF_INET6)
    rate = 4096
    config = replace(
        configuration(
            url4,
            workers_ipv4=2,
            workers_ipv6=2,
            download_mib_per_minute=rate * 60 / 1024**2,
            chunk_bytes=256,
            max_response_bytes=65536,
            targets=(Target(url4, "ipv4"), Target(url6, "ipv6")),
        ),
        auth=AuthConfig(),
    )
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(family=socket.AF_INET6),
        cookie_jar=aiohttp.DummyCookieJar(),
        auto_decompress=False,
        trust_env=False,
    ) as session6:
        runner = TrafficRunner(
            config,
            {"ipv4": sessions["ipv4"], "ipv6": session6},
            lambda _: True,
            request_interval=0.01,
        )
        started = monotonic()
        task = asyncio.create_task(runner.run())
        try:
            await until(lambda: all(v["requests"] == 2 for v in runner.snapshot()["bulk"].values()))
            await asyncio.sleep(2)
        finally:
            await cancel(task)
        elapsed = monotonic() - started
        counts = [v["bytes"] for v in runner.snapshot()["bulk"].values()]
        assert sum(counts) <= rate * elapsed + runner.snapshot()["burst_bytes"] + 1
        assert min(counts) > rate * elapsed * 0.25
        assert abs(counts[0] - counts[1]) <= 4 * config.bulk.chunk_bytes
        assert not session6.connector._acquired
        assert not sessions["ipv4"].connector._acquired


@pytest.mark.asyncio
async def test_offline_transition_stops_an_existing_bulk_read_and_can_resume(http_server, sessions):
    online = True

    async def handler(request):
        return web.Response(body=b"x" * 4096)

    app = web.Application()
    app.router.add_get("/", handler)
    runner = TrafficRunner(
        configuration(await http_server(app), download_mib_per_minute=128 * 60 / 1024**2),
        sessions,
        lambda _: online,
        request_interval=0.01,
    )
    task = asyncio.create_task(runner.run())
    try:
        await until(lambda: runner.snapshot()["bulk"]["ipv4"]["bytes"] == 128)
        online = False
        await until(lambda: runner.snapshot()["bulk"]["ipv4"]["aborted"] == 1)
        stats = runner.snapshot()["bulk"]["ipv4"]
        assert stats["bytes"] == 128
        assert stats["requests"] == 1
        assert not sessions["ipv4"].connector._acquired
        await asyncio.sleep(0.05)
        assert runner.snapshot()["bulk"]["ipv4"]["requests"] == 1
        online = True
        await until(lambda: runner.snapshot()["bulk"]["ipv4"]["requests"] == 2)
    finally:
        await cancel(task)


@pytest.mark.asyncio
async def test_consecutive_failures_back_off_and_success_resets_streak(http_server, sessions):
    fail = True

    async def handler(request):
        return web.Response(status=503) if fail else web.Response(body=b"ok")

    app = web.Application()
    app.router.add_get("/", handler)
    now = 100.0
    runner = TrafficRunner(
        configuration(await http_server(app)), sessions, lambda _: True, clock=lambda: now
    )
    for delay in (5, 10, 20):
        await fetch_once(runner)
        assert runner.snapshot()["targets"]["bulk"][0]["cooldown_seconds"] == delay
        now += delay
    fail = False
    await fetch_once(runner)
    assert runner.snapshot()["targets"]["bulk"][0]["failures"] == 0
    fail = True
    await fetch_once(runner)
    assert runner.snapshot()["targets"]["bulk"][0]["cooldown_seconds"] == 5


@pytest.mark.asyncio
async def test_probe_still_observes_ipv6_after_ipv4_transport_failure(http_server, sessions):
    async def handler(request):
        return web.Response(body=b"v6")

    app = web.Application()
    app.router.add_get("/", handler)
    url = await http_server(app, family=socket.AF_INET6)
    config = Config(probe=ProbeConfig(interval_seconds=10, targets=(Target(url, "both"),)))
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(family=socket.AF_INET6),
        cookie_jar=aiohttp.DummyCookieJar(),
        auto_decompress=False,
        trust_env=False,
    ) as session6:
        runner = TrafficRunner(
            config, {"ipv4": sessions["ipv4"], "ipv6": session6}, lambda _: False
        )
        task = asyncio.create_task(runner.run())
        try:
            await until(lambda: runner.snapshot()["probe"]["ipv6"]["successes"] == 1)
            assert runner.snapshot()["probe"]["ipv4"]["last_result"] == "network_error"
            assert runner.snapshot()["probe"]["ipv6"]["bytes"] == 2
        finally:
            await cancel(task)


@pytest.mark.asyncio
async def test_probe_429_prevents_second_stack_request(http_server, sessions):
    hits = 0

    async def handler(request):
        nonlocal hits
        hits += 1
        return web.Response(status=429, headers={"Retry-After": "120"})

    app = web.Application()
    app.router.add_get("/", handler)
    url = await http_server(app)
    config = Config(probe=ProbeConfig(interval_seconds=0.01, targets=(Target(url, "both"),)))
    runner = TrafficRunner(
        config, {"ipv4": sessions["ipv4"], "ipv6": sessions["ipv4"]}, lambda _: False
    )
    task = asyncio.create_task(runner.run())
    try:
        await until(lambda: runner.snapshot()["probe"]["ipv4"]["failures"] == 1)
        await asyncio.sleep(0.05)
        assert hits == 1
        assert runner.snapshot()["probe"]["ipv6"]["requests"] == 0
    finally:
        await cancel(task)


@pytest.mark.asyncio
async def test_same_url_dns_failure_does_not_interrupt_other_stack(http_server):
    class Resolver(aiohttp.abc.AbstractResolver):
        async def resolve(self, host, port=0, family=socket.AF_INET):
            if family == socket.AF_INET:
                raise socket.gaierror(socket.EAI_NONAME, "synthetic v4 DNS failure")
            return [
                {
                    "hostname": host,
                    "host": "::1",
                    "port": port,
                    "family": family,
                    "proto": 0,
                    "flags": 0,
                }
            ]

        async def close(self):
            pass

    async def handler(request):
        return web.Response(body=b"x" * 512)

    server = web.Application()
    server.router.add_get("/", handler)
    base = await http_server(server, family=socket.AF_INET6)
    url = f"http://synthetic-target.test:{urlsplit(base).port}/"
    config = replace(
        configuration(
            url,
            workers_ipv6=1,
            download_mib_per_minute=256 * 60 / 1024**2,
            targets=(Target(url, "ipv4"), Target(url, "ipv6")),
        ),
        auth=AuthConfig(),
    )
    async with (
        aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(family=socket.AF_INET, resolver=Resolver()),
            trust_env=False,
        ) as v4,
        aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(family=socket.AF_INET6, resolver=Resolver()),
            trust_env=False,
        ) as v6,
    ):
        runner = TrafficRunner(config, {"ipv4": v4, "ipv6": v6}, lambda _: True)
        download = asyncio.create_task(fetch_once(runner, "ipv6", 1))
        try:
            await until(lambda: runner.snapshot()["bulk"]["ipv6"]["bytes"] >= 128)
            await fetch_once(runner, "ipv4", 0)
            await asyncio.wait_for(download, 4)
            stats = runner.snapshot()["bulk"]
            assert stats["ipv4"]["last_result"] == "network_error"
            assert stats["ipv6"]["successes"] == 1 and stats["ipv6"]["bytes"] == 512
            runner.reset()
            cooldown = runner.snapshot()["targets"]["bulk"][0]
            assert cooldown["transport"]["ipv4"]["cooldown_seconds"] > 0
            assert cooldown["transport"]["ipv6"]["failures"] == 0
        finally:
            if not download.done():
                await cancel(download)


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_size", [3, 128])
async def test_html_with_leading_comments_is_not_bulk_progress(http_server, sessions, chunk_size):
    async def handler(request):
        return web.Response(
            body=b"\xef\xbb\xbf <!-- portal notice -->\n<!-- another --> <html><form>login</form></html>",
            content_type="application/octet-stream",
        )

    server = web.Application()
    server.router.add_get("/", handler)
    runner = TrafficRunner(
        configuration(await http_server(server), chunk_bytes=chunk_size), sessions, lambda _: True
    )
    await fetch_once(runner)
    stats = runner.snapshot()["bulk"]["ipv4"]
    assert stats["successes"] == 0 and stats["last_result"] == "html"


@pytest.mark.asyncio
async def test_large_chunk_cannot_expand_stream_buffer_watermarks(
    http_server, sessions, monkeypatch
):
    reads = []
    original = aiohttp.StreamReader.read

    async def measured(self, n=-1):
        value = await original(self, n)
        reads.append((n, self.get_read_buffer_limits()))
        return value

    monkeypatch.setattr(aiohttp.StreamReader, "read", measured)

    async def handler(request):
        return web.Response(body=b"x" * 131072)

    server = web.Application()
    server.router.add_get("/", handler)
    runner = TrafficRunner(
        configuration(await http_server(server), chunk_bytes=65536, max_response_bytes=131072),
        sessions,
        lambda _: True,
    )
    await fetch_once(runner)
    assert runner.snapshot()["bulk"]["ipv4"]["bytes"] == 131072
    assert reads and all(0 < n <= 16384 and high <= 32768 for n, (_, high) in reads)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_dead_inner_worker_cannot_leave_traffic_running(sessions, monkeypatch, cancelled):
    runner = TrafficRunner(configuration("http://127.0.0.1/"), sessions, lambda _: True)

    async def stopped(family, worker):
        if cancelled:
            raise asyncio.CancelledError

    monkeypatch.setattr(runner, "_bulk_loop", stopped)
    with pytest.raises(ExceptionGroup):
        await asyncio.wait_for(runner.run(), 0.2)
    assert not runner._running
