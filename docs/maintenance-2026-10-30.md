# 2026-10-30 维护检查

本轮保持 `src/`、`tests/`、`docs/`、只读 `references/` 的既有职责，新增统一在线文档入口和 `.editorconfig`。没有修改认证协议、配置、运行代码或上游快照。

## 当前复验

环境：Windows、Python 3.13.12；从 `uv.lock` 安装 dev 依赖。

| 命令 | 结果 |
| --- | --- |
| `uv sync --locked --group dev` | 退出 0，24 个锁定包安装成功 |
| `.venv/Scripts/ruff.exe check .` | 退出 0 |
| `.venv/Scripts/ruff.exe format --check .` | 退出 0，43 文件 |
| `.venv/Scripts/python.exe -m mypy src/thuauth` | 退出 0，17 源文件 |
| `.venv/Scripts/python.exe -m pytest -q -ra` | 退出 0，850 passed、1 skipped，26.06 秒 |

跳过项为 Windows 不适用的 POSIX 文件权限检查。本轮未发现需要改动运行代码的已证实错误；测试通过不能证明不存在其他错误。

## 验证边界

未读取真实配置、未进行校园网登录或公网 bulk 下载；未重新生成 Go 对拍 fixture，未复验分发包。校园网端到端验收仍遵循 [campus-validation.md](campus-validation.md)。历史 2026-10-27 的其他 Python 版本、CI 与构建记录保留在 [HANDOFF.md](../HANDOFF.md)，不能冒充本轮复验。

[在线文档](https://xiaoshecode.github.io/xiaoshecode/repos/thuauth/)汇总安装、目录职责和检查限制。
