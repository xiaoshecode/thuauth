"""Strict JSON configuration with immutable defaults and atomic private writes."""

from __future__ import annotations

import csv
import io
import ipaddress
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from thuauth.errors import ConfigError
from thuauth.models import Credentials, Target, TargetFamily

MAX_CONFIG_BYTES = 1024 * 1024
MAX_WORKERS = 32


@dataclass(frozen=True, slots=True)
class AuthConfig:
    ipv4: bool = True
    ipv6: bool = True
    auto_relogin: bool = True
    health_interval_seconds: float = 30
    ac_id_ipv4: str | None = None
    ac_id_ipv6: str | None = None


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    interval_seconds: float = 20
    max_response_bytes: int = 32768
    targets: tuple[Target, ...] = (
        Target("https://info.tsinghua.edu.cn/", "both"),
        Target("https://www.baidu.com/"),
        Target("https://www.bing.com/"),
    )


@dataclass(frozen=True, slots=True)
class BulkConfig:
    enabled: bool = False
    download_mib_per_minute: float = 10
    workers_ipv4: int = 1
    workers_ipv6: int = 1
    chunk_bytes: int = 16384
    max_response_bytes: int = 1048576
    targets: tuple[Target, ...] = ()


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str = "INFO"
    file: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class Config:
    schema_version: int = 1
    credentials: Credentials = field(default_factory=Credentials, repr=False)
    auth: AuthConfig = field(default_factory=AuthConfig)
    probe: ProbeConfig = field(default_factory=ProbeConfig)
    bulk: BulkConfig = field(default_factory=BulkConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def require_credentials(self) -> None:
        if not self.credentials.username:
            raise ConfigError("$.username", "必须填写学号或账号")
        if not self.credentials.password:
            raise ConfigError("$.password", "必须填写密码")


_KNOWN_FIELDS = {
    "$": {"schema_version", "username", "password", "auth", "probe", "bulk", "logging"},
    "$.auth": set(AuthConfig.__dataclass_fields__),
    "$.probe": set(ProbeConfig.__dataclass_fields__),
    "$.bulk": set(BulkConfig.__dataclass_fields__),
    "$.logging": set(LoggingConfig.__dataclass_fields__),
    "$.probe.targets[]": {"url", "family"},
    "$.bulk.targets[]": {"url", "family"},
}


def _field(parent: str, key: str) -> str:
    # Even an identifier-shaped unknown key may be a mistakenly pasted password.
    schema_path = re.sub(r"\[\d+\]", "[]", parent)
    name = key if key in _KNOWN_FIELDS.get(schema_path, set()) else "<unknown>"
    return f"{parent}.{name}"


def _object(value: object, path: str, allowed: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ConfigError(path, "必须是 JSON 对象")
    data = cast(dict[str, object], value)
    for key in data:
        if key not in allowed:
            raise ConfigError(_field(path, key), "未知字段")
    return data


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(path, "必须是布尔值 true 或 false")
    return value


def _integer(value: object, path: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(path, "必须是整数")
    if not minimum <= value <= maximum:
        raise ConfigError(path, f"必须介于 {minimum} 和 {maximum} 之间")
    return value


def _number(value: object, path: str, minimum: float, maximum: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(path, "必须是数值")
    if not minimum <= value <= maximum or not math.isfinite(value):
        raise ConfigError(path, f"必须是 {minimum:g} 到 {maximum:g} 之间的有限数值")
    return float(value)


def _text(value: object, path: str, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise ConfigError(path, "必须是字符串")
    if len(value) > maximum:
        raise ConfigError(path, f"长度不能超过 {maximum}")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ConfigError(path, "包含无效的 Unicode 字符") from None
    return value


def _ac_id(value: object, path: str) -> str | None:
    if value is None:
        return None
    result = _text(value, path, 16)
    if not re.fullmatch(r"[0-9]+", result) or int(result) < 1:
        raise ConfigError(path, "必须是正整数形式的字符串或 null")
    return result


def _url(value: object, path: str) -> str:
    result = _text(value, path)
    if not result or any(char.isspace() or ord(char) < 32 for char in result):
        raise ConfigError(path, "URL 不能为空或包含空白、控制字符")
    try:
        parsed = urlsplit(result)
        host = parsed.hostname
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or "#" in result
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
        ):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            ascii_host = host.encode("idna").decode("ascii").removesuffix(".")
            if len(ascii_host) > 253 or not all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in ascii_host.split(".")
            ):
                raise ValueError from None
    except (ValueError, UnicodeError):
        raise ConfigError(path, "必须是无内嵌账号密码及片段的有效 HTTP(S) URL") from None
    return result


def _targets(value: object, path: str, *, allow_both: bool) -> tuple[Target, ...]:
    if not isinstance(value, list) or len(value) > 64:
        raise ConfigError(path, "必须是最多 64 项的数组")
    targets = []
    families = {"ipv4", "ipv6", "both"} if allow_both else {"ipv4", "ipv6"}
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        entry = _object(item, item_path, {"url", "family"})
        family = _text(entry.get("family", "ipv4"), f"{item_path}.family", 16)
        if family not in families:
            raise ConfigError(f"{item_path}.family", "不支持的地址族")
        targets.append(
            Target(_url(entry.get("url"), f"{item_path}.url"), cast(TargetFamily, family))
        )
    return tuple(targets)


def parse_config(value: object) -> Config:
    """Parse known fields only. Optional defaults never replace explicit false or []."""
    root = _object(
        value, "$", {"schema_version", "username", "password", "auth", "probe", "bulk", "logging"}
    )
    version = _integer(root.get("schema_version", 1), "$.schema_version", 1, 1)
    username = _text(root.get("username", ""), "$.username", 256)
    if any(char.isspace() or ord(char) < 32 for char in username):
        raise ConfigError("$.username", "账号不能包含空白或控制字符")
    credentials = Credentials(username, _text(root.get("password", ""), "$.password"))

    a = _object(root.get("auth", {}), "$.auth", set(AuthConfig.__dataclass_fields__))
    auth = AuthConfig(
        ipv4=_boolean(a.get("ipv4", True), "$.auth.ipv4"),
        ipv6=_boolean(a.get("ipv6", True), "$.auth.ipv6"),
        auto_relogin=_boolean(a.get("auto_relogin", True), "$.auth.auto_relogin"),
        health_interval_seconds=_number(
            a.get("health_interval_seconds", 30), "$.auth.health_interval_seconds", 1, 3600
        ),
        ac_id_ipv4=_ac_id(a.get("ac_id_ipv4"), "$.auth.ac_id_ipv4"),
        ac_id_ipv6=_ac_id(a.get("ac_id_ipv6"), "$.auth.ac_id_ipv6"),
    )
    if not auth.ipv4 and not auth.ipv6:
        raise ConfigError("$.auth", "至少启用一个地址族")

    p = _object(root.get("probe", {}), "$.probe", set(ProbeConfig.__dataclass_fields__))
    probe = ProbeConfig(
        interval_seconds=_number(
            p.get("interval_seconds", 20), "$.probe.interval_seconds", 1, 86400
        ),
        max_response_bytes=_integer(
            p.get("max_response_bytes", 32768), "$.probe.max_response_bytes", 1, 1048576
        ),
        targets=(
            _targets(p["targets"], "$.probe.targets", allow_both=True)
            if "targets" in p
            else ProbeConfig().targets
        ),
    )

    b = _object(root.get("bulk", {}), "$.bulk", set(BulkConfig.__dataclass_fields__))
    bulk = BulkConfig(
        enabled=_boolean(b.get("enabled", False), "$.bulk.enabled"),
        download_mib_per_minute=_number(
            b.get("download_mib_per_minute", 10), "$.bulk.download_mib_per_minute", 0.01, 1048576
        ),
        workers_ipv4=_integer(b.get("workers_ipv4", 1), "$.bulk.workers_ipv4", 0, MAX_WORKERS),
        workers_ipv6=_integer(b.get("workers_ipv6", 1), "$.bulk.workers_ipv6", 0, MAX_WORKERS),
        chunk_bytes=_integer(b.get("chunk_bytes", 16384), "$.bulk.chunk_bytes", 1, 1048576),
        max_response_bytes=_integer(
            b.get("max_response_bytes", 1048576), "$.bulk.max_response_bytes", 1, 1073741824
        ),
        targets=_targets(b.get("targets", []), "$.bulk.targets", allow_both=False),
    )
    if bulk.workers_ipv4 + bulk.workers_ipv6 > MAX_WORKERS:
        raise ConfigError("$.bulk", f"worker 总数不能超过 {MAX_WORKERS}")
    if bulk.chunk_bytes > bulk.max_response_bytes:
        raise ConfigError("$.bulk.chunk_bytes", "不能超过单次响应体上限")
    if bulk.enabled:
        active = {
            family
            for family, enabled, count in (
                ("ipv4", auth.ipv4, bulk.workers_ipv4),
                ("ipv6", auth.ipv6, bulk.workers_ipv6),
            )
            if enabled and count
        }
        if not active:
            raise ConfigError("$.bulk", "至少为一个已启用地址族配置 worker")
        if not active.issubset({target.family for target in bulk.targets}):
            raise ConfigError("$.bulk.targets", "每个有活跃 worker 的地址族必须配置下载目标")

    log = _object(root.get("logging", {}), "$.logging", {"level", "file"})
    level = _text(log.get("level", "INFO"), "$.logging.level", 16)
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError("$.logging.level", "不支持的日志级别")
    log_file = log.get("file")
    if log_file is not None:
        log_file = _text(log_file, "$.logging.file")
        if not log_file or any(ord(char) < 32 for char in log_file):
            raise ConfigError("$.logging.file", "必须是非空路径或 null")
    return Config(version, credentials, auth, probe, bulk, LoggingConfig(level, log_file))


class _ObjectPairs(list[tuple[str, object]]):
    """Preserve duplicate JSON keys until their full field path is known."""


def _materialize(value: object, path: str = "$") -> object:
    if isinstance(value, _ObjectPairs):
        result: dict[str, object] = {}
        for key, item in value:
            child = _field(path, key)
            if key in result:
                raise ConfigError(child, "重复字段")
            result[key] = _materialize(item, child)
        return result
    if isinstance(value, list):
        return [_materialize(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError(path, "不允许 NaN 或 Infinity")
    return value


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        raise ConfigError("$", "无法读取配置文件，请检查路径和权限；首次使用请运行 init") from None
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError("$", "配置文件不能超过 1 MiB")
    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_ObjectPairs)
        return parse_config(_materialize(value))
    except json.JSONDecodeError as exc:
        raise ConfigError("$", f"JSON 格式错误（第 {exc.lineno} 行，第 {exc.colno} 列）") from None
    except (UnicodeError, ValueError, RecursionError):
        raise ConfigError("$", "配置必须是有效的 UTF-8 JSON，且嵌套深度不能过大") from None


def config_to_dict(config: Config) -> dict[str, object]:
    """Serialize for local storage only: the returned mapping contains the password."""
    return {
        "schema_version": config.schema_version,
        "username": config.credentials.username,
        "password": config.credentials.password,
        "auth": asdict(config.auth),
        "probe": {**asdict(config.probe), "targets": [asdict(t) for t in config.probe.targets]},
        "bulk": {**asdict(config.bulk), "targets": [asdict(t) for t in config.bulk.targets]},
        "logging": asdict(config.logging),
    }


def default_config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", ""))
        if not base.is_absolute():
            base = Path.home() / "AppData" / "Roaming"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", ""))
        if not base.is_absolute():
            base = Path.home() / ".config"
    return base / "thuauth" / "config.json"


def _restrict_windows_file(path: Path) -> None:
    """Apply a current-user ACL to the empty temp file before writing any credentials."""
    system32 = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"
    try:
        identity = subprocess.run(
            [str(system32 / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            check=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        row = next(csv.reader(io.StringIO(identity.stdout.decode("utf-8", errors="replace"))))
        sid = row[1]
        if not re.fullmatch(r"S-[0-9]+(?:-[0-9]+)+", sid):
            raise ValueError
        subprocess.run(
            [str(system32 / "icacls.exe"), str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"],
            capture_output=True,
            check=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, StopIteration):
        raise ConfigError("$", "无法设置 Windows 私有文件权限，未写入凭据") from None


def save_config(config: Config, path: Path, *, overwrite: bool = False) -> None:
    """Publish a private temp file atomically; by default even a racing creator wins."""
    data = config_to_dict(config)
    parse_config(data)
    payload = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    if len(payload) > MAX_CONFIG_BYTES:
        raise ConfigError("$", "配置文件不能超过 1 MiB")
    temporary: Path | None = None
    try:
        if path.is_symlink():
            raise ConfigError("$", "拒绝写入符号链接配置路径")
        if path.exists() and not overwrite:
            raise ConfigError("$", "配置已存在；如需覆盖请显式使用 --force")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".thuauth-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            if sys.platform == "win32":
                _restrict_windows_file(temporary)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            # An existence check plus replace would silently overwrite a racing writer.
            os.link(temporary, path)
    except FileExistsError:
        raise ConfigError("$", "配置已存在；如需覆盖请显式使用 --force") from None
    except OSError:
        raise ConfigError("$", "无法原子写入配置，请检查目录权限及文件系统支持") from None
    finally:
        if temporary is not None:
            failure = sys.exception()
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                message = "临时配置清理失败；请在配置目录检查并删除 .thuauth-*.tmp（可能含凭据）"
                if isinstance(failure, ConfigError):
                    raise ConfigError(failure.field, f"{failure.message}；{message}") from None
                if failure is None:
                    raise ConfigError("$", f"配置已写入，但{message}") from None
                # Preserve cancellation and unexpected programming errors.
                failure.add_note(message)
