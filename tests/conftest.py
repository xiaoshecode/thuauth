"""Loopback-only aiohttp servers shared by transport and integration tests."""

import socket

import pytest
import pytest_asyncio
from aiohttp import web


@pytest_asyncio.fixture
async def http_server():
    runners = []

    async def start(app: web.Application, family: int = socket.AF_INET) -> str:
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        runners.append(runner)
        host = "::1" if family == socket.AF_INET6 else "127.0.0.1"
        site = web.TCPSite(runner, host, 0)
        try:
            await site.start()
        except OSError:
            if family == socket.AF_INET6:
                pytest.skip("IPv6 loopback is unavailable on this Windows host")
            raise
        address = runner.addresses[0]
        return f"http://[{host}]:{address[1]}" if family == socket.AF_INET6 else f"http://{host}:{address[1]}"

    yield start
    for runner in reversed(runners):
        await runner.cleanup()
