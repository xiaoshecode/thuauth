import json
import logging
from unittest.mock import AsyncMock

import pytest

from thuauth import cli
from thuauth.errors import ConfigError, InstanceBusyError, NetworkError, ProtocolError
from thuauth.logging_setup import safe_traceback

SECRET = "synthetic-private-cli-value"


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "test.json"
    path.write_text(
        json.dumps({"username": "synthetic-user", "password": SECRET}), encoding="utf-8"
    )
    return path


@pytest.mark.parametrize("command", ["login", "logout", "status", "doctor", "keep"])
def test_runtime_commands_dispatch_validated_config(monkeypatch, config_path, capsys, command):
    run = AsyncMock(return_value=0)
    monkeypatch.setattr(cli, "_run_network", run)
    assert cli.main([command, "-c", str(config_path), "--no-ipv6", "--json"]) == 0
    cfg, args = run.call_args.args
    assert cfg.auth.ipv4 is True and cfg.auth.ipv6 is False
    assert args.json and args.command == command
    assert SECRET not in str(capsys.readouterr())


def test_keep_overrides_preserve_explicit_false_and_global_rate(monkeypatch, config_path):
    run = AsyncMock(return_value=0)
    monkeypatch.setattr(cli, "_run_network", run)
    assert (
        cli.main(
            [
                "keep",
                "-c",
                str(config_path),
                "--no-auto-relogin",
                "--no-bulk",
                "--rate",
                "20",
                "--workers-ipv4",
                "3",
                "--duration",
                "0.1",
            ]
        )
        == 0
    )
    cfg, args = run.call_args.args
    assert not cfg.auth.auto_relogin and not cfg.bulk.enabled
    assert cfg.bulk.download_mib_per_minute == 20
    assert cfg.bulk.workers_ipv4 == 3
    assert args.duration == 0.1
    assert "download_mib_per_minute" not in json.loads(config_path.read_text())


@pytest.mark.parametrize(
    "options",
    [
        ["--duration", "nan"],
        ["--duration", "0"],
        ["--duration", "-1"],
        ["--duration", "86401"],
        ["--rate", "inf"],
        ["--workers-ipv4", "33"],
        ["--no-ipv4", "--no-ipv6"],
        ["--bulk"],
    ],
)
def test_bad_runtime_options_never_reach_network(monkeypatch, config_path, options):
    run = AsyncMock(side_effect=AssertionError("must not reach network"))
    monkeypatch.setattr(cli, "_run_network", run)
    assert cli.main(["keep", "-c", str(config_path), *options]) == 2
    run.assert_not_called()


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ConfigError("$", "invalid"), 2),
        (InstanceBusyError(), 5),
        (NetworkError("timeout"), 4),
        (ProtocolError("invalid_response"), 4),
        (RuntimeError(SECRET), 1),
        (KeyboardInterrupt(SECRET), 130),
    ],
)
def test_runtime_failures_have_safe_exit_codes(monkeypatch, config_path, capsys, error, code):
    monkeypatch.setattr(cli, "_run_network", AsyncMock(side_effect=error))
    assert cli.main(["status", "-c", str(config_path)]) == code
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert captured.err
    assert not logging.getLogger("thuauth").handlers


def test_debug_log_is_rotated_and_closed(monkeypatch, config_path, tmp_path):
    log = tmp_path / "diagnostics.log"
    data = json.loads(config_path.read_text())
    data["logging"] = {"file": str(log)}
    config_path.write_text(json.dumps(data), encoding="utf-8")

    async def run(cfg, args):
        logging.getLogger("thuauth.test").debug("synthetic_debug_event")
        return 0

    monkeypatch.setattr(cli, "_run_network", run)
    assert cli.main(["--debug", "status", "-c", str(config_path)]) == 0
    content = log.read_text(encoding="utf-8")
    assert "synthetic_debug_event" in content and SECRET not in content
    # Windows cannot rename an open RotatingFileHandler file.
    log.rename(tmp_path / "renamed.log")


def test_unexpected_exception_group_omits_message_context_and_locals():
    try:
        try:
            raise ValueError(SECRET)
        except ValueError as cause:
            raise ExceptionGroup(SECRET, [RuntimeError(SECRET)]) from cause
    except Exception as error:
        detail = safe_traceback(error)
    assert "ExceptionGroup" in detail and "RuntimeError" in detail
    assert "test_cli_runtime.py" in detail
    assert SECRET not in detail
