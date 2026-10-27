"""Actual command wiring against a synthetic local portal; no campus access."""

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web

from thuauth import app
from thuauth.config import AuthConfig, BulkConfig, Config, ProbeConfig
from thuauth.errors import ConfigError, InstanceBusyError
from thuauth.http import SrunClient
from thuauth.models import Credentials, Target
from thuauth.protocol.srun import CALLBACK

USER = "synthetic-integration-user"
SECRET = "synthetic-integration-password"


@pytest_asyncio.fixture
async def portal(http_server, monkeypatch, tmp_path):
    state = {"owner": None, "operations": [], "requests": 0}
    online = asyncio.Event()

    async def handler(request):
        state["requests"] += 1
        if request.path == "/srun_portal_pc":
            return web.Response(text='var config = {ip: "192.0.2.17"};')
        if request.path == "/cgi-bin/rad_user_info":
            data = (
                {"error": "ok", "user_name": state["owner"]}
                if state["owner"]
                else {"error": "not_online"}
            )
            if state["owner"]:
                online.set()
        elif request.path == "/cgi-bin/get_challenge":
            data = {"res": "ok", "challenge": "synthetic-token-0123456789abcdef"}
        elif request.path == "/cgi-bin/srun_portal":
            action = request.query["action"]
            state["operations"].append(action)
            state["owner"] = request.query["username"] if action == "login" else None
            data = {"error": "ok"}
        else:
            raise AssertionError(f"Unexpected test path: {request.path}")
        return web.Response(text=f"{CALLBACK}({json.dumps(data)})")

    server = web.Application()
    server.router.add_get("/{path:.*}", handler)
    base = await http_server(server)

    def client(family, provider, **kwargs):
        return SrunClient(family, provider, ac_id="1", base_url=base)

    monkeypatch.setattr(app, "SrunClient", client)
    monkeypatch.setattr(app, "runtime_lock_path", lambda: tmp_path / "runtime.lock")
    config = Config(
        credentials=Credentials(USER, SECRET),
        auth=AuthConfig(ipv6=False),
        probe=ProbeConfig(targets=()),
    )
    return config, state, online


@pytest.mark.asyncio
async def test_login_status_logout_end_to_end(portal, capsys):
    config, state, _ = portal
    assert await app.run_command(config, "status", as_json=True) == 4
    assert await app.run_command(config, "login", as_json=True) == 0
    assert await app.run_command(config, "login", as_json=True) == 0
    assert await app.run_command(config, "status", as_json=True) == 0
    # Logout only needs ownership, never a password.
    config = replace(config, credentials=Credentials(USER, ""))
    assert await app.run_command(config, "logout", as_json=True) == 0
    assert state["operations"] == ["login", "logout"]
    output = capsys.readouterr().out
    assert USER not in output and SECRET not in output
    reports = [json.loads(line) for line in output.splitlines()]
    assert [item["families"][0]["state"] for item in reports] == [
        "offline",
        "online",
        "online",
        "online",
        "offline",
    ]
    assert reports[1]["families"][0]["username"] == "***"


@pytest.mark.asyncio
async def test_doctor_never_authenticates(portal, capsys):
    config, state, _ = portal
    assert await app.run_command(config, "doctor", as_json=True) == 4
    report = json.loads(capsys.readouterr().out)
    assert report["diagnostics"]["ipv4"]["portal"] == "ok"
    assert not state["operations"]


@pytest.mark.asyncio
async def test_doctor_target_failure_is_visible_and_nonzero(portal, http_server, capsys):
    config, state, _ = portal
    state["owner"] = USER
    requests = []

    async def unavailable(request):
        requests.append(request.headers.get("Range"))
        return web.Response(status=503)

    server = web.Application()
    server.router.add_get("/file", unavailable)
    base = await http_server(server)
    config = replace(config, probe=ProbeConfig(targets=(Target(base + "/file"),)))
    assert await app.run_command(config, "doctor", as_json=True) == 4
    report = json.loads(capsys.readouterr().out)
    assert report["families"][0]["state"] == "online"
    assert report["targets"][0]["status"] == 503
    assert requests == ["bytes=0-1023"] and not state["operations"]


@pytest.mark.asyncio
async def test_keep_integrates_auth_and_real_bulk(portal, http_server, monkeypatch, capsys):
    config, state, _ = portal

    async def file(request):
        return web.Response(body=b"x" * 32768, content_type="application/octet-stream")

    server = web.Application()
    server.router.add_get("/file", file)
    base = await http_server(server)
    config = replace(
        config,
        bulk=BulkConfig(
            enabled=True,
            workers_ipv6=0,
            chunk_bytes=4096,
            max_response_bytes=32768,
            targets=(Target(base + "/file"),),
        ),
    )
    runners = []
    real_runner = app.TrafficRunner

    def runner(*args):
        instance = real_runner(*args)
        runners.append(instance)
        return instance

    monkeypatch.setattr(app, "TrafficRunner", runner)
    stop = asyncio.Event()
    task = asyncio.create_task(app.run_command(config, "keep", as_json=True, stop_event=stop))
    try:
        async with asyncio.timeout(5):
            while not runners or runners[0].snapshot()["bulk"]["ipv4"]["successes"] == 0:
                await asyncio.sleep(0.01)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    report = json.loads(capsys.readouterr().out)
    assert report["traffic"]["bulk"]["ipv4"]["bytes"] >= 32768
    assert state["operations"] == ["login"] and state["owner"] == USER


@pytest.mark.asyncio
async def test_keep_lock_allows_status_and_releases_without_logout(portal, capsys):
    config, state, online = portal
    stop = asyncio.Event()
    task = asyncio.create_task(app.run_command(config, "keep", as_json=True, stop_event=stop))
    try:
        await asyncio.wait_for(online.wait(), 5)
        requests = state["requests"]
        with pytest.raises(InstanceBusyError):
            await app.run_command(config, "logout")
        assert state["requests"] == requests
        assert await app.run_command(config, "status", as_json=True) == 0
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    assert state["owner"] == USER and state["operations"] == ["login"]
    assert await app.run_command(config, "logout", as_json=True) == 0
    assert state["operations"] == ["login", "logout"]
    assert SECRET not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_keep_without_auto_relogin_stays_offline(portal):
    config, state, _ = portal
    config = replace(config, auth=replace(config.auth, auto_relogin=False))
    assert await app.run_command(config, "keep", duration=0.15) == 4
    assert not state["operations"]


@pytest.mark.asyncio
async def test_cancel_keep_releases_resources_and_lock(portal):
    config, state, online = portal
    task = asyncio.create_task(app.run_command(config, "keep"))
    await asyncio.wait_for(online.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    with app.instance_lock():
        pass
    assert state["operations"] == ["login"]


@pytest.mark.asyncio
async def test_missing_credentials_fail_before_network(portal):
    config, state, _ = portal
    with pytest.raises(ConfigError):
        await app.run_command(replace(config, credentials=Credentials()), "login")
    assert state["requests"] == 0


@pytest.mark.asyncio
async def test_windows_process_lock_survives_owner_crash(tmp_path, monkeypatch):
    lock_path = tmp_path / "process.lock"
    monkeypatch.setattr(app, "runtime_lock_path", lambda: lock_path)
    script = "\n".join(
        [
            "import sys, time",
            "from pathlib import Path",
            "from thuauth import app",
            "app.runtime_lock_path = lambda: Path(sys.argv[1])",
            "with app.instance_lock():",
            "    print('locked', flush=True)",
            "    time.sleep(60)",
        ]
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(lock_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        assert (await asyncio.wait_for(process.stdout.readline(), 10)).strip() == b"locked"
        with pytest.raises(InstanceBusyError), app.instance_lock():
            pytest.fail("Another process owns this lock")
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.communicate(), 5)
    with app.instance_lock():
        pass


def test_lock_path_is_user_scoped(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "default_config_path", lambda: tmp_path / "config.json")
    assert app.runtime_lock_path() == tmp_path / "runtime.lock"


def test_lock_permission_error_is_sanitized(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "runtime_lock_path", lambda: tmp_path / "runtime.lock")

    def denied(*args, **kwargs):
        raise PermissionError(SECRET)

    monkeypatch.setattr(Path, "mkdir", denied)
    with pytest.raises(ConfigError) as caught, app.instance_lock():
        pass
    assert SECRET not in str(caught.value)
