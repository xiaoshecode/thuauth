from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from thuauth import config as config_module
from thuauth.config import (
    Config,
    config_to_dict,
    default_config_path,
    load_config,
    parse_config,
    save_config,
)
from thuauth.errors import ConfigError
from thuauth.models import Credentials


def test_example_matches_defaults() -> None:
    example = Path(__file__).parents[1] / "config.example.json"
    assert load_config(example) == Config()


def test_defaults_are_immutable_and_false_and_empty_list_survive() -> None:
    cfg = parse_config({"auth": {"ipv6": False, "auto_relogin": False}, "probe": {"targets": []}})
    assert cfg.auth.ipv4
    assert not cfg.auth.ipv6
    assert not cfg.auth.auto_relogin
    assert cfg.probe.targets == ()
    assert len(Config().probe.targets) == 3
    with pytest.raises(FrozenInstanceError):
        cfg.auth.ipv6 = True


@pytest.mark.parametrize(
    ("data", "field"),
    [
        ([], "$"),
        ({"schema_version": 2}, "$.schema_version"),
        ({"schema_version": True}, "$.schema_version"),
        ({"typo": 1}, "$.<unknown>"),
        ({"auth": {"ipv6": "false"}}, "$.auth.ipv6"),
        ({"auth": {"ipv4": 0}}, "$.auth.ipv4"),
        ({"auth": {"ipv4": False, "ipv6": False}}, "$.auth"),
        ({"auth": {"health_interval_seconds": 0}}, "$.auth.health_interval_seconds"),
        ({"auth": {"ac_id_ipv4": 1}}, "$.auth.ac_id_ipv4"),
        ({"auth": {"ac_id_ipv6": "0"}}, "$.auth.ac_id_ipv6"),
        ({"auth": None}, "$.auth"),
        ({"username": "account name"}, "$.username"),
        ({"password": None}, "$.password"),
        ({"password": "\ud800"}, "$.password"),
        ({"bulk": {"workers_ipv4": True}}, "$.bulk.workers_ipv4"),
        ({"bulk": {"workers_ipv6": 1.5}}, "$.bulk.workers_ipv6"),
        ({"bulk": {"workers_ipv4": 17, "workers_ipv6": 16}}, "$.bulk"),
        ({"bulk": {"download_mib_per_minute": -1}}, "$.bulk.download_mib_per_minute"),
        ({"bulk": {"download_mib_per_minute": float("inf")}}, "$.bulk.download_mib_per_minute"),
        ({"bulk": {"download_mib_per_minute": float("nan")}}, "$.bulk.download_mib_per_minute"),
        ({"bulk": {"download_mib_per_minute": 10**500}}, "$.bulk.download_mib_per_minute"),
        ({"bulk": {"max_response_bytes": 10}}, "$.bulk.chunk_bytes"),
        ({"bulk": {"enabled": True}}, "$.bulk.targets"),
        ({"bulk": {"enabled": True, "workers_ipv4": 0, "workers_ipv6": 0}}, "$.bulk"),
        ({"probe": {"targets": {}}}, "$.probe.targets"),
        (
            {"probe": {"targets": [{"url": "https://example.test", "x": 1}]}},
            "$.probe.targets[0].<unknown>",
        ),
        ({"logging": {"level": "info"}}, "$.logging.level"),
        ({"logging": {"file": ""}}, "$.logging.file"),
    ],
)
def test_invalid_fields_are_localized(data: object, field: str) -> None:
    with pytest.raises(ConfigError) as caught:
        parse_config(data)
    assert caught.value.field == field


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/file",
        "https:///no-host",
        "https://user:private-value@example.test/file",
        "https://example.test:99999",
        "https://example.test:0",
        "https://[invalid]/",
        "https://bad_host/",
        "https://example.test/#secret",
        "https://example.test/\nprivate-value",
    ],
)
def test_invalid_target_does_not_echo_url(url: str) -> None:
    with pytest.raises(ConfigError) as caught:
        parse_config({"probe": {"targets": [{"url": url}]}})
    assert caught.value.field == "$.probe.targets[0].url"
    assert url not in str(caught.value)
    assert "private-value" not in str(caught.value)


def test_ipv6_url_and_active_family_target_rules() -> None:
    cfg = parse_config(
        {
            "auth": {"ipv4": False},
            "bulk": {
                "enabled": True,
                "targets": [{"url": "https://[2001:db8::1]/file.bin", "family": "ipv6"}],
            },
        }
    )
    assert cfg.bulk.targets[0].family == "ipv6"
    with pytest.raises(ConfigError, match=r"\$\.bulk.targets"):
        parse_config({"bulk": {"enabled": True, "targets": [{"url": "https://example.test/"}]}})


@pytest.mark.parametrize(
    ("contents", "field"),
    [
        ('{"password":"private-value", "password":"another"}', "$.password"),
        ('{"auth":{"ipv6":true,"ipv6":false}}', "$.auth.ipv6"),
        ('{"probe":{"targets":[{"url":"x","url":"y"}]}}', "$.probe.targets[0].url"),
        ('{"bulk":{"download_mib_per_minute":NaN}}', "$.bulk.download_mib_per_minute"),
        ('{"bulk":{"download_mib_per_minute":1e999}}', "$.bulk.download_mib_per_minute"),
        ('{"password":"private-value",}', "$"),
        ('{"password":"private-value","schema_version":' + "9" * 5000 + "}", "$"),
        ("[" * 2000 + "0" + "]" * 2000, "$"),
    ],
)
def test_invalid_json_is_bounded_and_redacted(tmp_path: Path, contents: str, field: str) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    assert caught.value.field == field
    assert "private-value" not in str(caught.value)


def test_missing_directory_invalid_encoding_and_oversized_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.json")
    with pytest.raises(ConfigError):
        load_config(tmp_path)
    path = tmp_path / "invalid.json"
    path.write_bytes(b"\xff")
    with pytest.raises(ConfigError):
        load_config(path)
    path.write_bytes(b" " * (config_module.MAX_CONFIG_BYTES + 1))
    with pytest.raises(ConfigError, match="1 MiB"):
        load_config(path)


def test_bom_and_credentials_check_do_not_strip_password(tmp_path: Path) -> None:
    password = "  synthetic-密码  "
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"username": "synthetic", "password": password}), encoding="utf-8-sig"
    )
    cfg = load_config(path)
    cfg.require_credentials()
    assert cfg.credentials.password == password
    assert password not in repr(cfg)
    assert password not in repr(cfg.credentials)
    assert "synthetic" not in repr(cfg.credentials)
    with pytest.raises(ConfigError, match=r"\$\.username"):
        Config().require_credentials()
    with pytest.raises(ConfigError, match=r"\$\.password"):
        Config(credentials=Credentials("synthetic")).require_credentials()


def test_round_trip_and_no_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "new" / "config.json"
    cfg = Config(credentials=Credentials("synthetic", "  private-value  "))
    save_config(cfg, path)
    assert load_config(path) == cfg
    assert parse_config(config_to_dict(cfg)) == cfg
    original = path.read_bytes()
    with pytest.raises(ConfigError):
        save_config(Config(), path)
    assert path.read_bytes() == original
    save_config(Config(), path, overwrite=True)
    assert load_config(path) == Config()
    assert not list(path.parent.glob(".thuauth-*.tmp"))


def test_failed_replace_preserves_original_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.json"
    path.write_bytes(b"original")

    def fail_replace(*args: object) -> None:
        raise OSError("synthetic failure")

    monkeypatch.setattr(config_module.os, "replace", fail_replace)
    with pytest.raises(ConfigError):
        save_config(
            Config(credentials=Credentials("synthetic", "private-value")), path, overwrite=True
        )
    assert path.read_bytes() == b"original"
    assert not list(tmp_path.glob(".thuauth-*.tmp"))


def test_racing_creator_is_not_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.json"
    original_link = os.link

    def racing_link(source: Path, target: Path) -> None:
        target.write_bytes(b"other process won")
        original_link(source, target)

    monkeypatch.setattr(config_module.os, "link", racing_link)
    with pytest.raises(ConfigError):
        save_config(Config(), path)
    assert path.read_bytes() == b"other process won"
    assert not list(tmp_path.glob(".thuauth-*.tmp"))


def test_acl_failure_prevents_writing_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_module.sys, "platform", "win32")

    def fail_acl(path: Path) -> None:
        assert path.read_bytes() == b""
        raise ConfigError("$", "synthetic ACL failure")

    monkeypatch.setattr(config_module, "_restrict_windows_file", fail_acl)
    with pytest.raises(ConfigError):
        save_config(
            Config(credentials=Credentials("synthetic", "private-value")), tmp_path / "c.json"
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions require a POSIX host")
def test_posix_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_config(Config(), path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs require a Windows host")
def test_windows_acl_has_no_inherited_or_broad_access(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_config(Config(), path)
    system32 = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"
    script = (
        "$ErrorActionPreference='Stop'; $a=Get-Acl -LiteralPath $env:THUAUTH_ACL_TEST_PATH; "
        "[PSCustomObject]@{Protected=$a.AreAccessRulesProtected; "
        "Rules=@($a.Access | ForEach-Object { "
        "[PSCustomObject]@{Inherited=$_.IsInherited; "
        "Sid=$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value} "
        "})} | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        [
            str(system32 / "WindowsPowerShell/v1.0/powershell.exe"),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        # PS7's module path is incompatible with the system Windows PowerShell 5.1.
        env={
            **{key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"},
            "THUAUTH_ACL_TEST_PATH": str(path),
        },
        capture_output=True,
        check=True,
        timeout=15,
    )
    acl = json.loads(result.stdout)
    assert acl["Protected"]
    assert acl["Rules"]
    assert not any(rule["Inherited"] for rule in acl["Rules"])
    assert not {"S-1-1-0", "S-1-5-11", "S-1-5-32-545"}.intersection(
        rule["Sid"] for rule in acl["Rules"]
    )


def test_platform_config_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert default_config_path() == tmp_path / "thuauth/config.json"
    monkeypatch.setattr(config_module.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert default_config_path() == tmp_path / "thuauth/config.json"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-not-a-config-root")
    monkeypatch.setattr(config_module.Path, "home", lambda: tmp_path)
    assert default_config_path() == tmp_path / ".config/thuauth/config.json"
