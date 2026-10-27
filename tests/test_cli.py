"""M0 CLI contract tests; all credentials and configuration paths are synthetic."""

from __future__ import annotations

import builtins
import getpass
import inspect
import json
import os
import shutil
import socket
import subprocess
import sys
import sysconfig
import warnings
from pathlib import Path
from unittest.mock import Mock

import pytest

from thuauth import cli, config
from thuauth.models import Credentials

USERNAME = "synthetic-student-m0"
PASSWORD = "  synthetic-password-密碼-Ω  "
BAD_URL = "https://synthetic-user:synthetic-url-secret@example.invalid/private?token=m0"


def _patch_dependency(monkeypatch, module, name, replacement):
    """Patch the public dependency and any already-imported CLI alias."""
    original = getattr(module, name)
    monkeypatch.setattr(module, name, replacement)
    for alias, value in vars(cli).copy().items():
        if value is original:
            monkeypatch.setattr(cli, alias, replacement)


@pytest.fixture(autouse=True)
def isolated_cli(monkeypatch, tmp_path):
    # Even a regression in default-path handling must not read the user's config.
    for variable in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(variable, str(tmp_path / "fake-home"))
    default_path = tmp_path / "default-config.json"
    _patch_dependency(monkeypatch, config, "default_config_path", lambda: default_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)

    def unexpected_input(*args, **kwargs):
        pytest.fail("This command must not request interactive input")

    def unexpected_network(*args, **kwargs):
        pytest.fail("M0 commands must not perform network operations")

    monkeypatch.setattr(builtins, "input", unexpected_input)
    _patch_dependency(monkeypatch, getpass, "getpass", unexpected_input)
    monkeypatch.setattr(socket, "create_connection", unexpected_network)
    monkeypatch.setattr(socket, "getaddrinfo", unexpected_network)
    monkeypatch.setattr(socket.socket, "connect", unexpected_network)
    monkeypatch.setattr(socket.socket, "connect_ex", unexpected_network)
    return default_path


def _interactive(monkeypatch, username=USERNAME, passwords=(PASSWORD, PASSWORD)):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    account_input = Mock(return_value=username)
    password_input = Mock(side_effect=passwords)
    monkeypatch.setattr(builtins, "input", account_input)
    _patch_dependency(monkeypatch, getpass, "getpass", password_input)
    return account_input, password_input


def _write_config(path, username=USERNAME, password=PASSWORD, **extra):
    data = {"schema_version": 1, "username": username, "password": password, **extra}
    raw = json.dumps(data, ensure_ascii=False)
    path.write_text(raw, encoding="utf-8")
    return raw


def _with_path(command, flag, position, path):
    args = list(command)
    args[position:position] = [flag, str(path)]
    return args


def _assert_result(args, expected):
    # Only --help/--version may escape as SystemExit; errors must return an int.
    result = cli.main(args)
    assert type(result) is int
    assert result == expected


def _assert_private(capsys, caplog, *extra):
    captured = capsys.readouterr()
    output = captured.out + captured.err + caplog.text
    for secret in (USERNAME, PASSWORD, PASSWORD.strip(), BAD_URL, *extra):
        if secret:
            assert secret not in output
            assert json.dumps(secret, ensure_ascii=True)[1:-1] not in output
    assert "Traceback (most recent call last)" not in output
    return captured


@pytest.mark.parametrize("command", [[], ["init"], ["config"], ["config", "check"]])
def test_help_exits_successfully_without_reading_config(monkeypatch, capsys, command):
    reader = Mock(side_effect=AssertionError("Help must not load configuration"))
    _patch_dependency(monkeypatch, config, "load_config", reader)
    try:
        result = cli.main([*command, "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        assert type(result) is int
        assert result == 0
    assert "--help" in capsys.readouterr().out
    reader.assert_not_called()


def test_version_exits_successfully_without_reading_config(monkeypatch, capsys):
    reader = Mock(side_effect=AssertionError("Version must not load configuration"))
    _patch_dependency(monkeypatch, config, "load_config", reader)
    try:
        result = cli.main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        assert type(result) is int
        assert result == 0
    assert capsys.readouterr().out.strip()
    reader.assert_not_called()


@pytest.mark.parametrize(
    "args",
    [
        ["unknown-command"],
        ["config"],
        ["config", "unknown-subcommand"],
        ["--unknown-option"],
        ["init", "--unknown-option"],
        ["config", "check", "--unknown-option"],
        ["--config"],
        ["init", "--config"],
        ["config", "--config"],
        ["config", "check", "--config"],
        ["init", "--require-credentials"],
        ["config", "check", "--force"],
        ["config", "check", "unexpected-positional"],
    ],
)
def test_usage_errors_return_two_without_touching_config(monkeypatch, tmp_path, capsys, args):
    reader = Mock(side_effect=AssertionError("Invalid arguments must not load configuration"))
    writer = Mock(side_effect=AssertionError("Invalid arguments must not save configuration"))
    _patch_dependency(monkeypatch, config, "load_config", reader)
    _patch_dependency(monkeypatch, config, "save_config", writer)
    _assert_result(args, 2)
    assert capsys.readouterr().err.strip()
    reader.assert_not_called()
    writer.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "args",
    [
        ["init", "--password", "SyntheticSecret123"],
        ["config", "check", "--password=SyntheticSecret123"],
        ["SyntheticSecret123"],
        ["config", "SyntheticSecret123"],
    ],
)
def test_parser_errors_never_echo_arguments(monkeypatch, tmp_path, capsys, caplog, args):
    reader = Mock(side_effect=AssertionError("Invalid arguments must not load configuration"))
    writer = Mock(side_effect=AssertionError("Invalid arguments must not save configuration"))
    _patch_dependency(monkeypatch, config, "load_config", reader)
    _patch_dependency(monkeypatch, config, "save_config", writer)
    _assert_result(args, 2)
    assert _assert_private(capsys, caplog, "SyntheticSecret123").err.strip()
    reader.assert_not_called()
    writer.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "raw",
    [
        '{"password":"SyntheticSecret123","SyntheticSecret123":0}',
        '{"password":{"SyntheticSecret123":0,"SyntheticSecret123":1}}',
        '{"auth":{"SyntheticSecret123":0}}',
    ],
)
def test_unknown_json_keys_are_redacted(tmp_path, capsys, caplog, raw):
    path = tmp_path / "invalid.json"
    path.write_text(raw, encoding="utf-8")
    _assert_result(["config", "check", "-c", str(path)], 2)
    assert "<unknown>" in _assert_private(capsys, caplog, "SyntheticSecret123").err


@pytest.mark.parametrize("publication_fails", [False, True])
def test_cleanup_failure_is_reported_without_masking_publication_error(
    monkeypatch, tmp_path, capsys, caplog, publication_fails
):
    path = tmp_path / "new-config.json"
    _interactive(monkeypatch)
    original_unlink = Path.unlink
    original_link = os.link

    def fail_cleanup(candidate, *args, **kwargs):
        if candidate.name.startswith(".thuauth-"):
            raise PermissionError(PASSWORD)
        return original_unlink(candidate, *args, **kwargs)

    def publish(source, destination, *args, **kwargs):
        if publication_fails:
            raise PermissionError(PASSWORD)
        return original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    monkeypatch.setattr(os, "link", publish)
    _assert_result(["init", "-c", str(path)], 2)
    output = _assert_private(capsys, caplog).err
    assert "临时" in output
    assert ("无法原子写入" if publication_fails else "已写入") in output
    assert path.exists() is not publication_fails
    leftovers = list(tmp_path.glob(".thuauth-*.tmp"))
    assert len(leftovers) == 1
    assert config.load_config(leftovers[0]).credentials.password == PASSWORD
    # The injected failure is temporary; remove synthetic credentials after inspection.
    original_unlink(leftovers[0])


@pytest.mark.parametrize("flag", ["-c", "--config"])
@pytest.mark.parametrize("position", [0, 1, 2], ids=["root", "after-config", "after-check"])
def test_check_accepts_config_path_at_each_parser_level(
    monkeypatch, tmp_path, capsys, caplog, flag, position
):
    path = tmp_path / "selected config.json"
    raw = _write_config(path)
    original_reader = config.load_config
    reader = Mock(wraps=original_reader)
    _patch_dependency(monkeypatch, config, "load_config", reader)
    _assert_result(_with_path(["config", "check"], flag, position, path), 0)
    reader.assert_called_once()
    call = inspect.signature(original_reader).bind(
        *reader.call_args.args, **reader.call_args.kwargs
    )
    assert Path(call.arguments["path"]) == path
    _assert_private(capsys, caplog, raw)
    assert path.read_text(encoding="utf-8") == raw


def test_check_uses_default_config_path(isolated_cli, capsys, caplog):
    raw = _write_config(isolated_cli)
    _assert_result(["config", "check"], 0)
    _assert_private(capsys, caplog, raw)


def test_main_accepts_none_and_reads_sys_argv(monkeypatch, tmp_path):
    path = tmp_path / "argv.json"
    _write_config(path)
    monkeypatch.setattr(sys, "argv", ["thuauth", "config", "check", "-c", str(path)])
    result = cli.main()
    assert type(result) is int
    assert result == 0


def test_main_accepts_a_tuple_sequence_without_mutating_it(tmp_path):
    path = tmp_path / "tuple.json"
    _write_config(path)
    args = ("config", "check", "-c", str(path))
    _assert_result(args, 0)
    assert args == ("config", "check", "-c", str(path))


@pytest.mark.parametrize("data", [{}, {"username": "", "password": ""}])
def test_check_allows_unfilled_credentials_by_default(tmp_path, data):
    path = tmp_path / "empty.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    _assert_result(["config", "check", "-c", str(path)], 0)


@pytest.mark.parametrize(
    ("username", "password", "expected"),
    [
        ("", "", 2),
        ("", PASSWORD, 2),
        (USERNAME, "", 2),
        (USERNAME, PASSWORD, 0),
        (USERNAME, "   ", 0),
    ],
    ids=["both-empty", "username-empty", "password-empty", "filled", "password-spaces"],
)
def test_require_credentials_checks_only_nonempty_fields_locally(
    tmp_path, capsys, caplog, username, password, expected
):
    path = tmp_path / "credentials.json"
    raw = _write_config(path, username, password)
    _assert_result(["config", "check", "-c", str(path), "--require-credentials"], expected)
    _assert_private(capsys, caplog, raw)
    assert path.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize("kind", ["missing", "directory", "bad-json", "bad-url", "bad-type"])
def test_check_config_errors_are_private_and_return_two(tmp_path, capsys, caplog, kind):
    path = tmp_path / "invalid.json"
    raw = ""
    if kind == "directory":
        path.mkdir()
    elif kind == "bad-json":
        raw = json.dumps({"username": USERNAME, "password": PASSWORD}, ensure_ascii=False)[:-1]
        path.write_text(raw, encoding="utf-8")
    elif kind == "bad-url":
        raw = _write_config(path, probe={"targets": [{"url": BAD_URL, "family": "ipv4"}]})
    elif kind == "bad-type":
        raw = _write_config(path, auth={"ipv4": PASSWORD})
    _assert_result(["config", "check", "-c", str(path)], 2)
    captured = _assert_private(capsys, caplog, raw, "synthetic-url-secret", "token=m0")
    assert captured.err.strip()
    if raw:
        assert path.read_text(encoding="utf-8") == raw
    elif kind == "missing":
        assert not path.exists()


@pytest.mark.parametrize("flag", ["-c", "--config"])
@pytest.mark.parametrize("position", [0, 1], ids=["root", "after-init"])
def test_init_uses_public_save_contract_and_preserves_password(
    monkeypatch, tmp_path, capsys, caplog, flag, position
):
    path = tmp_path / "new config.json"
    account_input, password_input = _interactive(monkeypatch, username=f" \t{USERNAME}\n ")
    original_writer = config.save_config
    writer = Mock(wraps=original_writer)
    _patch_dependency(monkeypatch, config, "save_config", writer)
    _assert_result(_with_path(["init"], flag, position, path), 0)
    account_input.assert_called_once()
    assert password_input.call_count == 2
    writer.assert_called_once()
    call = inspect.signature(original_writer).bind(
        *writer.call_args.args, **writer.call_args.kwargs
    )
    call.apply_defaults()
    assert Path(call.arguments["path"]) == path
    assert call.arguments["overwrite"] is False
    assert call.arguments["config"] == config.Config(credentials=Credentials(USERNAME, PASSWORD))
    saved = config.load_config(path)
    assert saved.credentials == Credentials(USERNAME, PASSWORD)
    assert set(tmp_path.iterdir()) == {path}
    _assert_private(capsys, caplog, path.read_text(encoding="utf-8"))


def test_init_uses_default_config_path(monkeypatch, isolated_cli):
    _interactive(monkeypatch)
    _assert_result(["init"], 0)
    assert config.load_config(isolated_cli).credentials == Credentials(USERNAME, PASSWORD)


@pytest.mark.parametrize(
    ("stdin_tty", "stderr_tty"), [(False, False), (False, True), (True, False)]
)
@pytest.mark.parametrize("force", [False, True])
def test_init_requires_both_ttys_before_prompting_or_writing(
    monkeypatch, tmp_path, capsys, caplog, stdin_tty, stderr_tty, force
):
    path = tmp_path / "noninteractive.json"
    monkeypatch.setattr(sys.stdin, "isatty", lambda: stdin_tty)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: stderr_tty)
    args = ["init", "-c", str(path)] + (["--force"] if force else [])
    _assert_result(args, 2)
    assert list(tmp_path.iterdir()) == []
    assert _assert_private(capsys, caplog).err.strip()


@pytest.mark.parametrize(
    ("username", "passwords"),
    [
        (USERNAME, (PASSWORD, "different-synthetic-password")),
        (" \t ", (PASSWORD, PASSWORD)),
        (USERNAME, ("", "")),
    ],
    ids=["mismatch", "blank-username", "empty-password"],
)
def test_invalid_init_input_is_rejected_without_creating_files(
    monkeypatch, tmp_path, capsys, caplog, username, passwords
):
    path = tmp_path / "rejected.json"
    _interactive(monkeypatch, username, passwords)
    _assert_result(["init", "-c", str(path)], 2)
    assert list(tmp_path.iterdir()) == []
    assert _assert_private(capsys, caplog, "different-synthetic-password").err.strip()


@pytest.mark.parametrize("warning_at", [0, 1], ids=["password", "confirmation"])
def test_getpass_warning_aborts_before_unsafe_fallback(
    monkeypatch, tmp_path, capsys, caplog, warning_at
):
    path = tmp_path / "unsafe.json"
    account_input, _ = _interactive(monkeypatch)
    calls = 0

    def unsafe_getpass(*args, **kwargs):
        nonlocal calls
        index = calls
        calls += 1
        if index == warning_at:
            warnings.warn(f"unsafe fallback: {PASSWORD}", getpass.GetPassWarning, stacklevel=2)
            pytest.fail("GetPassWarning must abort before an echoing fallback can run")
        return PASSWORD

    _patch_dependency(monkeypatch, getpass, "getpass", unsafe_getpass)
    with warnings.catch_warnings(record=True) as leaked_warnings:
        warnings.simplefilter("always", getpass.GetPassWarning)
        _assert_result(["init", "-c", str(path)], 2)
    assert not leaked_warnings
    assert calls == warning_at + 1
    account_input.assert_called_once()
    assert list(tmp_path.iterdir()) == []
    assert _assert_private(capsys, caplog).err.strip()


@pytest.mark.parametrize("stage", ["username", "password", "confirmation"])
@pytest.mark.parametrize(("error", "expected"), [(EOFError, 2), (KeyboardInterrupt, 130)])
def test_input_eof_and_interrupt_return_codes_without_files(
    monkeypatch, tmp_path, capsys, caplog, stage, error, expected
):
    path = tmp_path / "interrupted.json"
    account_input, password_input = _interactive(monkeypatch)
    failure = error(f"synthetic exception must stay private: {PASSWORD}")
    if stage == "username":
        account_input.side_effect = failure
    else:
        password_input.side_effect = [failure] if stage == "password" else [PASSWORD, failure]
    _assert_result(["init", "-c", str(path)], expected)
    assert list(tmp_path.iterdir()) == []
    assert _assert_private(capsys, caplog).err.strip()


def test_existing_config_is_untouched_without_force_and_without_prompting(
    monkeypatch, tmp_path, capsys, caplog
):
    path = tmp_path / "existing.json"
    raw = _write_config(path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    writer = Mock(side_effect=AssertionError("Existing config must be rejected before saving"))
    _patch_dependency(monkeypatch, config, "save_config", writer)
    _assert_result(["init", "-c", str(path)], 2)
    writer.assert_not_called()
    assert path.read_text(encoding="utf-8") == raw
    assert set(tmp_path.iterdir()) == {path}
    assert _assert_private(capsys, caplog, raw).err.strip()


def test_force_resets_options_and_atomically_replaces_existing_config(
    monkeypatch, tmp_path, capsys, caplog
):
    path = tmp_path / "existing.json"
    original = _write_config(
        path,
        username="old-synthetic-user",
        password="old-synthetic-password",
        auth={"ipv6": False},
        probe={"targets": []},
        logging={"level": "DEBUG"},
    )
    _interactive(monkeypatch)
    original_writer = config.save_config
    writer = Mock(wraps=original_writer)
    _patch_dependency(monkeypatch, config, "save_config", writer)
    original_replace = os.replace
    publications = []

    def observe_replace(source, destination, *args, **kwargs):
        assert Path(destination) == path
        assert Path(source).parent == path.parent
        assert Path(source) != path
        assert path.read_text(encoding="utf-8") == original
        staged = json.loads(Path(source).read_text(encoding="utf-8"))
        assert staged["username"] == USERNAME
        assert staged["password"] == PASSWORD
        publications.append(Path(source))
        return original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", observe_replace)
    _assert_result(["init", "--force", "-c", str(path)], 0)
    writer.assert_called_once()
    call = inspect.signature(original_writer).bind(
        *writer.call_args.args, **writer.call_args.kwargs
    )
    assert call.arguments["overwrite"] is True
    assert len(publications) == 1
    assert config.load_config(path) == config.Config(credentials=Credentials(USERNAME, PASSWORD))
    assert set(tmp_path.iterdir()) == {path}
    _assert_private(capsys, caplog, original, "old-synthetic-user", "old-synthetic-password")


@pytest.mark.parametrize(("error", "expected"), [(PermissionError, 2), (KeyboardInterrupt, 130)])
def test_failed_force_publication_preserves_existing_file_and_cleans_temporary_file(
    monkeypatch, tmp_path, capsys, caplog, error, expected
):
    path = tmp_path / "existing.json"
    original = _write_config(path, password="old-synthetic-password")
    _interactive(monkeypatch)
    attempted = []

    def fail_replace(source, destination, *args, **kwargs):
        assert Path(destination) == path
        assert Path(source).parent == path.parent
        assert json.loads(Path(source).read_text(encoding="utf-8"))["password"] == PASSWORD
        attempted.append(Path(source))
        raise error(f"unsafe exception detail: {PASSWORD} {BAD_URL}")

    monkeypatch.setattr(os, "replace", fail_replace)
    _assert_result(["init", "--force", "-c", str(path)], expected)
    assert len(attempted) == 1
    assert path.read_text(encoding="utf-8") == original
    assert set(tmp_path.iterdir()) == {path}
    assert _assert_private(capsys, caplog, original, "old-synthetic-password").err.strip()


def test_check_keyboard_interrupt_returns_130(monkeypatch, tmp_path, capsys, caplog):
    reader = Mock(side_effect=KeyboardInterrupt(PASSWORD))
    _patch_dependency(monkeypatch, config, "load_config", reader)
    _assert_result(["config", "check", "-c", str(tmp_path / "unused.json")], 130)
    reader.assert_called_once()
    assert _assert_private(capsys, caplog).err.strip()


def _run_help(command, tmp_path):
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    # Run outside the checkout: these smoke tests exercise installed packaging.
    completed = subprocess.run(
        [*command, "--help"],
        cwd=tmp_path,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    for option in ("--help", "--version", "init", "config"):
        assert option in completed.stdout
    assert "Traceback (most recent call last)" not in completed.stderr
    assert list(tmp_path.iterdir()) == []


def test_python_module_help_subprocess(tmp_path):
    _run_help([sys.executable, "-m", "thuauth"], tmp_path)


def test_installed_console_entrypoint_help_subprocess(tmp_path):
    executable = shutil.which("thuauth", path=sysconfig.get_path("scripts"))
    assert executable is not None, "Install the project before running packaging smoke tests"
    _run_help([executable], tmp_path)
