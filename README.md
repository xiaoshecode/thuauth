# thuauth

使用 Codex 协作开发的个人清华校园网认证与保活工具，采用 Python 3.12+、asyncio 和 aiohttp，当前只交付 Windows。本机 JSON 保存学号密码，双栈分别检查、按需登录，默认自动重登，支持自选网站访问及可配置下载流量。

**M0–M4 的工程、协议、认证、保活与交付代码已实现。** 本机假服务器验证不等于学校门户验证；实际检查结果见 [HANDOFF.md](HANDOFF.md)，待用户执行的步骤见 [校园网验收](docs/campus-validation.md)。

## Windows 开始使用

准备 uv 与 Python 3.12+，在项目目录执行：

```powershell
uv sync --locked --group dev
uv run thuauth --help
uv run thuauth config check -c config.example.json
uv run thuauth init
uv run thuauth config check --require-credentials
uv run thuauth doctor
uv run thuauth login
uv run thuauth status --json
uv run thuauth keep
```

本机本轮安装的 uv 位于 `.tools/bin/uv.exe`，因此也可将上面的 `uv` 替换为 `./.tools/bin/uv.exe`；该目录和 `.venv` 不提交 Git，克隆到其他机器后需自行准备环境。

`init` 在交互终端隐藏输入密码并确认，默认保存在 Windows `%APPDATA%/thuauth/config.json`。密码按需求以明文保存，文件权限限制为当前用户。需要其他路径可加 `-c`；只有显式 `init --force` 才覆盖已有配置，并将其他选项重置到默认值。

`config check` 仅校验本地结构；`--require-credentials` 额外检查账号密码已填写，不证明密码正确。编辑配置可运行 `notepad "$env:APPDATA/thuauth/config.json"`。

**Ctrl+C 结束 keep，但保持校园网登录状态。** 要登出，先停止守护，再运行 `uv run thuauth logout`。keep/login/logout 使用同一个用户级锁，换 `-c` 也不会绕过；status/doctor 可以同时运行。

| 命令 | 行为 |
| --- | --- |
| `doctor --json` | 只读诊断 DNS、门户、TLS、状态及目标；不登录，每个目标/地址族至多读取 1 KiB |
| `status --json` | 分别报告在线、离线、未知、地址族不可用或账号冲突 |
| `login` | 检查两栈，串行补齐明确离线的栈，操作后复查 |
| `logout` | 只登出配置账号，不替换其他在线账号 |
| `keep` | 健康检查、自动重登、probe 与已启用的 bulk |

默认启用 IPv6；没有 IPv6 时 IPv4 仍工作，报告明确显示降级。只需要 IPv4 可用 `keep --no-ipv6`。`keep --no-auto-relogin` 禁止自动提交密码；所有 CLI 覆盖只影响本次运行，不改写 JSON。

## 自选网站与下载流量

默认 probe 每 20 秒轮询 info、百度、Bing 中的一个目标。`family: "both"` 分别探测双栈，可编辑 `probe.targets` 增删网站，空列表关闭 probe。页面跳转只记录、不跟随。

bulk 默认关闭且目标为空。在现有 JSON 的 `bulk` 字段中配置自己选定的文件服务，以下 URL 需替换后才能启用：

```json
{
  "enabled": true,
  "download_mib_per_minute": 10,
  "workers_ipv4": 2,
  "workers_ipv6": 2,
  "chunk_bytes": 16384,
  "max_response_bytes": 1048576,
  "targets": [
    {"url": "https://your-host.example/test.bin", "family": "ipv4"},
    {"url": "https://your-host.example/test.bin", "family": "ipv6"}
  ]
}
```

目标必须支持对应地址族。没有 IPv6 下载源时设 `workers_ipv6: 0`，仍可保留 IPv6 认证。先运行 config check 和 doctor，再运行：

```powershell
uv run thuauth keep --bulk --rate 10 --workers-ipv4 2 --workers-ipv6 2
# 有限运行五分钟，结束后保持登录
uv run thuauth keep --duration 300 --json
```

10 MiB/min 是全部 bulk worker **合计**的正文长期读取预算，约 170.67 KiB/s。增加并发不放大总速率；要增加总量用 `--rate 20`。下载产生少量上行请求和下行响应，不是上下行各 10 MiB/min。认证和 probe 单独统计；TCP/TLS、重传、预读不属于正文统计，网卡计量会有差异。

流式读取不落盘；200 忽略 Range 也按本地上限关闭。HTML、空内容、错误范围和重定向不计为成功下载。429 遵守 Retry-After，超长等待暂停目标，其他失败退避；认证 UNKNOWN 暂停对应栈 bulk。持续流量不能保证学校侧永不下线。

## 排错与退出码

先 config check，再 doctor --json、status --json。UNKNOWN 不会触发密码重试。自动发现 ac_id 失败时，确认本接入点的实际值后填写 `auth.ac_id_ipv4/ipv6`，不能猜数字。凭据错误/账号限制阻断本次进程的自动认证；修正配置、确认解除后重启。

`--debug` 开启应用调试级别；`logging.file` 可指定本机日志，按 1 MiB 滚动、保留 3 份。状态变化立即记录，每分钟汇总流量；异常保留类型和源码位置，不回显请求、响应或密码。目标用从 0 开始的配置索引表示。JSON 在 stdout、日志在 stderr；keep --json 结束时输出最终统计。

| 退出码 | 含义 |
| --- | --- |
| 0 | 启用的栈全部在线，或显式登出后全部离线 |
| 1 | 未预期程序错误，按脱敏 traceback 定位 |
| 2 | 配置、参数或交互错误 |
| 3 | 认证拒绝或账号冲突 |
| 4 | 离线、未知、部分栈不可用或诊断未通过 |
| 5 | 已有 login/logout/keep 持锁 |
| 130 | Ctrl+C 中断 |

学校要求的 MD5、SHA1、XEncode、JSONP 保持兼容，不能仅为“新协议”替换服务器算法。认证使用 HTTPS 并校验证书；匿名接入发现沿用参考项目的 HTTP 地址，不提交凭据。

## 从 Codex 继续

将此仓库克隆到本机，直接在 Codex 中打开 **thuauth 仓库根目录**，不要把 references/GoAuthing 当成工作项目。

仓库地址：[xiaoshecode/thuauth](https://github.com/xiaoshecode/thuauth)，当前 GitHub 可见性为公开。克隆即可取得设计、提示词与完整参考源码；真实配置与日志保持在本机。

打开项目后输入：

```text
请读取 AGENTS.md、HANDOFF.md 和 docs/prompts.md，核对实际工作区。
当前只支持 Windows。按本次具体需求修改，保留双栈状态、重试、取消和总配额契约。
先复现问题并补回归测试，再修复；不连接真实校园认证或启动公网 bulk。
运行受影响检查，更新 HANDOFF.md，报告未验证项。
```

如果本次只想审查，使用 [架构审查提示词](docs/prompts.md#继续审查架构)，它明确限定不开始写应用代码。

## 项目资料

| 文件 | 用途 |
| --- | --- |
| [AGENTS.md](AGENTS.md) | Codex 项目规则、代码约定与评审重点 |
| [HANDOFF.md](HANDOFF.md) | 已完成、未实现、待验证与下一个任务 |
| [docs/architecture.md](docs/architecture.md) | 完整架构、状态机、接口、配置与流量口径 |
| [docs/development.md](docs/development.md) | M0–M4 实施步骤、多 agent 分工与验收 |
| [docs/prompts.md](docs/prompts.md) | 接续、审查、施工、并行和交接提示词 |
| [docs/runtime-contract.md](docs/runtime-contract.md) | 实际运行时接口与生命周期 |
| [docs/campus-validation.md](docs/campus-validation.md) | 用户在校园网络中的验收步骤 |
| [config.example.json](config.example.json) | 无凭据的配置规格示例 |
| [tools/README.md](tools/README.md) | Go 对拍 fixture 的 Windows 离线再生成与验证 |
| [references/README.md](references/README.md) | 上游来源、固定提交和文件完整性 |
| [references/GoAuthing/](references/GoAuthing/) | 完整 Go 参考源码、测试、README、部署示例 |
| [docs/history/README.md](docs/history/README.md) | 迁移前提示词与设计的原样存档 |

## 功能约定

- 学号、密码由用户保存在本机 JSON，不放入仓库。
- 双栈分别观察、按需认证；查询失败不会直接触发重登。
- probe 低频访问 info、百度、Bing，也允许配置其他站点。
- bulk 目标为所有下载 worker 合计约 10 MiB/min，可配置速率、并发和目标；具体下载源待选择。
- “多开”通过单进程内多个 worker 实现，避免多个进程争抢认证。

## 开发检查

```powershell
uv sync --locked --group dev
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy src/thuauth
uv run --locked pytest -q -ra
uv build
```

Windows CI 使用合成凭据和本机服务器，不登录真实校园网。普通测试不要求 Go；仅 fixture 再生成需要 Go，见 tools/README.md。

## 许可和参考源码

仓库附有 [GPLv3 许可证](LICENSE) 和 [来源说明](NOTICE.md)。参考快照来自 [z4yx/GoAuthing](https://github.com/z4yx/GoAuthing) 的 `3730e252ca11e6abbd055d7850c417c5751c1efa`，保留原文件内容与许可。

快照不是 Git submodule，克隆本仓库即可获得全部参考材料。快照里的 GitHub Actions 位于 references/ 下，不会作为本项目的工作流自动运行。Python 实现位于 src/thuauth/，不直接修改参考快照。
