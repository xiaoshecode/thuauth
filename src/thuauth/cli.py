"""Local-only M0 command line. Authentication commands arrive in later milestones."""

from __future__ import annotations

import argparse
import getpass
import sys
import warnings
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path
from typing import NoReturn

from thuauth.config import Config, default_config_path, load_config, save_config
from thuauth.errors import ConfigError
from thuauth.models import Credentials


class _PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse's standard errors echo rejected arguments, which may be credentials.
        self.print_usage(sys.stderr)
        self.exit(2, "参数错误：请用 --help 查看当前命令用法；账号密码仅通过 init 交互输入。\n")


def _config_option(parser: argparse.ArgumentParser, *, root: bool = False) -> None:
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None if root else argparse.SUPPRESS,
        help="配置文件路径（默认使用用户配置目录）",
    )


def _parser() -> argparse.ArgumentParser:
    parser = _PrivateArgumentParser(prog="thuauth", description="清华校园网客户端：本地配置工具")
    _config_option(parser, root=True)
    parser.add_argument("--version", action="version", version=f"thuauth {version('thuauth')}")
    commands = parser.add_subparsers(dest="command")
    init = commands.add_parser("init", help="交互输入账号密码，创建本地私有配置")
    _config_option(init)
    init.add_argument("--force", action="store_true", help="原子替换已有配置（重置为默认选项）")
    config = commands.add_parser("config", help="本地配置操作，不发起认证")
    _config_option(config)
    checks = config.add_subparsers(dest="config_command", required=True)
    check = checks.add_parser("check", help="检查 JSON 结构、类型和字段约束")
    _config_option(check)
    check.add_argument("--require-credentials", action="store_true", help="同时要求账号密码已填写")
    return parser


def _initialize(path: Path, *, overwrite: bool) -> None:
    if (path.exists() or path.is_symlink()) and not overwrite:
        raise ConfigError("$", "配置已存在；如需覆盖请显式使用 --force")
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise ConfigError("$", "init 需要交互终端，以安全隐藏密码输入")
    username = input("学号或账号: ").strip()
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            password = getpass.getpass("密码: ")
            confirmation = getpass.getpass("再次输入密码: ")
        except getpass.GetPassWarning:
            raise ConfigError("$.password", "当前终端不能安全隐藏密码输入") from None
    if password != confirmation:
        raise ConfigError("$.password", "两次输入的密码不一致")
    config = Config(credentials=Credentials(username, password))
    config.require_credentials()
    save_config(config, path, overwrite=overwrite)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # Keep the callable API usable by tests and future UI integrations.
        return exc.code if isinstance(exc.code, int) else 0
    if args.command is None:
        parser.print_help()
        return 0
    try:
        path = args.config.expanduser() if args.config is not None else default_config_path()
        if args.command == "init":
            _initialize(path, overwrite=args.force)
            print("配置已创建；密码以明文保存在指定本地文件中。未进行网络认证。")
        else:
            config = load_config(path)
            if args.require_credentials:
                config.require_credentials()
            print("配置有效（仅本地校验，未验证账号密码或网络）。")
        return 0
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except EOFError:
        print("输入已结束，配置初始化未完成。", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("操作已取消。", file=sys.stderr)
        return 130
