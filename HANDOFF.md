# thuauth 当前进度与接续

更新日期：2026-10-26。主要开发工具：Codex。本文件只记录实际状态，计划中的功能不算已实现。

GitHub：[xiaoshecode/thuauth](https://github.com/xiaoshecode/thuauth)，私有仓库。本阶段工作分支为 `codex/m0`，基于 `codex/bootstrap` 的 `5315da9`；最新提交和同步情况以 Git 为准。

## 当前状态：M0 已完成

- 已建立 `pyproject.toml`、`uv.lock`、`src/thuauth/` 与 `tests/`。Python 最低 3.12，运行依赖为 aiohttp、filelock，开发检查使用 pytest、pytest-asyncio、ruff、mypy。
- 已实现不可变 Config、Credentials、Target 和 ConfigError；严格校验 JSON 类型、未知/重复字段、有限数值、URL、地址族与 bulk 跨字段约束。显式 false 和空列表得到保留。
- 已实现安装后 `thuauth` 与 `python -m thuauth` 两个入口，以及 `--help`、`--version`、`init`、`config check`。预期错误返回 2，用户中断返回 130。
- `init` 交互式读取并确认密码，在本机明文 JSON 保存。临时文件写入前设置权限：Windows 当前用户 ACL，POSIX 0600；默认通过原子硬链接发布且不覆盖已有文件，`--force` 使用原子替换。
- 主 agent 编写共享配置、CLI 和配置测试；独立 agent 编写 CLI 测试；只读 reviewer 发现的三项缺陷均已先复现、再修复并添加回归测试。
- 已更新 README、架构、实施计划、提示词与来源说明。未创建未来认证或保活模块的占位实现。
- 原有用户需求、历史提示词和参考仓库均保留。23 个参考文件与 SHA-256 清单逐项一致，参考目录没有修改。原始完整聊天未导出，历史目录不宣称包含全部对话。

## 本阶段明确的架构契约

1. M0 的 CLI 直接调用配置模块；应用编排和异步资源生命周期在 M2 引入，当前不需要空的 app 层。
2. `config check` 允许未填凭据的示例配置；`--require-credentials` 额外检查非空账号密码。两者只做本地校验，不能判断学校是否接受密码。
3. `init` 要求 stdin 与 stderr 均为交互终端；无法隐藏输入时拒绝继续。只清理账号首尾空白，密码空格与 Unicode 原样保存。`--force` 会重置全部其他选项为默认值。
4. 新文件通过同目录临时文件和原子硬链接发布，避免“检查存在后再替换”的竞态覆盖。文件系统不支持硬链接时明确失败；写入失败保留旧文件。
5. 配置错误只显示已知字段路径或 JSON 行列；未知字段名称显示 `<unknown>`。命令行解析错误不回显参数；临时文件清理失败保留原始失败原因，并明确提示可能残留含凭据的 `.thuauth-*.tmp`。
6. bulk 默认关闭，目标列表为空；启用后必须为每个实际工作的地址族提供目标。最多 32 个 worker，所有 worker 未来共享 10 MiB/min 默认正文预算。禁用地址族的 worker 数配置可以保留，但不参与活跃目标要求。

## 实际检查结果

环境：Windows、Python 3.12.14、uv 0.13.5；本次验证了最低 Python 次版本。uv 安装在本机忽略目录 `.tools/bin/uv.exe`，没有加入全局 PATH；以下 `uv` 实际使用该路径，设置 `UV_CACHE_DIR=.uv-cache`。解释器来自 Codex 附带运行时，本机 `.venv` 已建立。锁文件同步解析到 24 个包。

| 命令或检查 | 退出码 | 实际结果 |
| --- | --- | --- |
| `uv sync --locked --group dev` | 0 | 锁文件与安装环境一致 |
| `uv run --locked ruff check .` | 0 | 静态检查通过 |
| `uv run --locked ruff format --check .` | 0 | 格式通过；架构文档 Python 代码块也纳入检查 |
| `uv run --locked mypy src/thuauth` | 0 | 6 个源文件通过 strict 类型检查 |
| `uv run --locked pytest -q -ra` | 0 | 138 passed，1 skipped |
| `uv build --offline` | 0 | 构建 wheel 和 sdist 成功 |
| 构建包内容核对 | 0 | wheel 包含 CLI 入口和许可，sdist 包含测试与配置示例；没有本地缓存、真实配置和参考目录 |
| `git diff --exit-code -- references` 与 SHA-256 清单核对 | 0 | 23 个参考文件全部匹配 |

测试覆盖配置错误路径、凭据不回显、重复键与畸形 JSON、配置大小、默认值、目标约束、交互终止与密码不一致、写入失败旧文件不变、并发创建不覆盖、写前 ACL 和临时清理失败；还在仓库目录之外启动安装后的两个 CLI 入口。

唯一跳过项是 `test_posix_private_permissions`：当前 Windows 主机不能验证 POSIX 文件权限。Windows ACL 测试实际调用系统 ACL 工具检查，已通过。

复核新增的 9 个回归用例在修复前全部失败，修复后全部通过，已包含在 138 项中。另一次全量格式检查发现文档代码块格式问题，格式化后重跑通过。早期沙箱启动 Python 入口遇到 uv trampoline / 临时目录限制，最终检查在获准的本机执行环境中完成；Windows ACL 测试子进程的 PowerShell 模块路径冲突也已修正。这些早期失败不当作通过结果。

构建输出位于忽略目录 `dist/`：`thuauth-0.1.0-py3-none-any.whl` 与 `thuauth-0.1.0.tar.gz`。构建工具提示本地缓存目录在源码树内；已按构建包实际成员检查，缓存未被打包。

## 尚未实现或验证

- M1–M4 尚未完成：没有协议编码/HTTP 客户端、双栈在线检查、认证协调、自动重登、probe、bulk、限速器、守护循环、进程锁、诊断或 CI。
- 没有真实校园网认证或公网 bulk 请求。当前只是保存 IPv4/IPv6、自动重登和流量参数，不表示这些运行行为已实现。
- 尚未选定持续下载源，bulk.enabled 为 false。info、百度、Bing 仅为未来低频探测目标，不能自动当作持续下载源。
- 尚未在 Linux/macOS、其他 Python 次版本或其他文件系统运行。POSIX 权限、跨平台安装和无硬链接文件系统需后续验证。
- getpass 交互使用合成输入模拟终端测试，尚未由用户在实际交互终端手动验证；没有输入或读取用户真实账号密码。
- 崩溃或断电级持久性、文件系统拒绝清理后的人工恢复未做实机故障注入。正常写入、替换失败、并发创建与清理失败已有自动化覆盖。

## 待校内验证的设计问题

- double_stack=1 是否使一次认证同时开通另一栈，以及 auth4/auth6 状态生效延迟。
- auth6 门户的实际 IP 字段格式；原 Go 正则仅匹配 IPv4 文本。
- 空 IP、challenge 返回 IP、最终签名 IP 的绑定规则；ac_id 自动探测在不同接入网络的行为。
- 主动节流与 aiohttp 读超时、预读缓冲的交互，先用本地服务验证。
- 持续下载源及其 IPv6 支持。这不阻塞 M1–M2。

## 下一个可执行任务：M1 协议

1. 读取 references/GoAuthing 的编码、参数和 JSONP 实现，保持参考文件只读，保留来源说明。
2. 实现纯字节 XEncode/SRBX1、登录登出参数与 JSONP 解析；建立合成 Go/Python 完整报文对拍 fixture。
3. 覆盖 uint32 回绕、字节序、Unicode、JSON 转义、MD5/checksum、短 key、畸形响应与错误分类。
4. 暂不引入真实 HTTP 认证或保活；网络和双栈状态属于 M2，持续流量属于 M3。
5. 运行受影响检查与里程碑全套检查，更新本文件，保留校内实测的未知项。

```text
请读取 AGENTS.md、HANDOFF.md 和 docs/prompts.md，核对实际文件与 Git 状态。
沿用 M0 配置契约，按 docs/development.md 完成 M1 协议实现和 Go/Python 对拍测试。
先处理影响实现的具体设计问题，不改参考快照，不连接真实认证服务。
运行真实检查，更新 HANDOFF.md，报告结果与未验证项。
```
