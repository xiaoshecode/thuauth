"""Windows application wiring: one process lock, explicit sessions and safe reports."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from functools import partial
from pathlib import Path

from filelock import FileLock, Timeout

from thuauth.auth import AuthService, enabled_families
from thuauth.config import Config, default_config_path
from thuauth.daemon import Daemon
from thuauth.doctor import diagnose_targets
from thuauth.errors import ConfigError, InstanceBusyError
from thuauth.http import SessionPool, SrunClient
from thuauth.models import IPFamily, OnlineObservation, OnlineState
from thuauth.traffic import TrafficRunner


def runtime_lock_path() -> Path:
    # Intentionally independent of -c: alternate config files share this lock.
    return default_config_path().parent / "runtime.lock"


@contextmanager
def instance_lock() -> Iterator[None]:
    path = runtime_lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(path, timeout=0)
        lock.acquire()
    except Timeout:
        raise InstanceBusyError from None
    except OSError:
        raise ConfigError("$", "无法创建运行锁，请检查用户配置目录权限") from None
    try:
        yield
    finally:
        lock.release()


def _report(observations: tuple[OnlineObservation, ...]) -> list[dict[str, object]]:
    return [
        {
            "family": item.family,
            "state": item.state.value,
            "reason": item.reason,
            "username": "***" if item.username else None,
            "ip": item.ip,
        }
        for item in observations
    ]


def _exit_code(observations: tuple[OnlineObservation, ...], *, logout: bool = False) -> int:
    if any(item.state is OnlineState.BLOCKED for item in observations):
        return 3
    expected = OnlineState.OFFLINE if logout else OnlineState.ONLINE
    return 0 if observations and all(item.state is expected for item in observations) else 4


def _output(report: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False))
        return
    print("连接方式：直连；TLS 校验开启；IPv4/IPv6 分别检查。")
    families = report.get("families")
    if isinstance(families, list):
        for item in families:
            if isinstance(item, dict):
                print(f"{item['family']}: {item['state']} ({item['reason'] or 'checked'})")
    for name in ("diagnostics", "targets", "traffic"):
        if name in report:
            print(f"{name}: {json.dumps(report[name], ensure_ascii=False)}")


async def run_command(
    config: Config,
    command: str,
    *,
    as_json: bool = False,
    duration: float | None = None,
    stop_event: asyncio.Event | None = None,
) -> int:
    if command == "login" or (command == "keep" and config.auth.auto_relogin):
        config.require_credentials()
    if command in {"keep", "logout"} and not config.credentials.username:
        raise ConfigError("$.username", "必须填写账号，以避免操作其他在线账号")
    if command not in {"login", "logout", "status", "doctor", "keep"}:
        raise ValueError("unsupported application command")
    guard = instance_lock() if command in {"login", "logout", "keep"} else nullcontext()
    with guard:
        async with SessionPool(enabled_families(config)) as pool:
            clients: dict[IPFamily, SrunClient] = {}
            for family in enabled_families(config):
                clients[family] = SrunClient(
                    family,
                    partial(pool.auth.__getitem__, family),
                    ac_id=config.auth.ac_id_ipv4 if family == "ipv4" else config.auth.ac_id_ipv6,
                )
            service = AuthService(config, clients)
            report: dict[str, object] = {"schema_version": 1, "command": command}
            diagnostic_ok = True
            if command == "doctor":
                diagnostics = {
                    family: await client.diagnose() for family, client in clients.items()
                }
                targets = await diagnose_targets(config, pool.traffic)
                report["diagnostics"] = diagnostics
                report["targets"] = targets
                diagnostic_ok = all(
                    item["dns"] == item["portal"] == item["tls"] == "ok"
                    and item["ac_id"] in {"ok", "configured"}
                    for item in diagnostics.values()
                ) and all(item["result"] == "ok" for item in targets)
                observations = await service.status_all()
            elif command == "status":
                observations = await service.status_all()
            elif command == "login":
                observations = await service.ensure_all()
            elif command == "logout":
                observations = await service.logout_all()
            else:
                stop = stop_event if stop_event is not None else asyncio.Event()
                traffic = TrafficRunner(config, pool.traffic, service.is_online)
                daemon = Daemon(service, traffic, pool.reset)

                async def finish_after_duration() -> None:
                    if duration is not None:
                        await asyncio.sleep(duration)
                        stop.set()

                timer = asyncio.create_task(finish_after_duration())
                try:
                    await daemon.run(stop)
                finally:
                    timer.cancel()
                    await asyncio.gather(timer, return_exceptions=True)
                observations = service.observations
                report["traffic"] = traffic.snapshot()
            report["families"] = _report(observations)
            _output(report, as_json=as_json)
            result = _exit_code(observations, logout=command == "logout")
            return 4 if result == 0 and not diagnostic_ok else result
