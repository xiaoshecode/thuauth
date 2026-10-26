# thuauth 当前进度与接续

更新日期：2026-10-26。主要开发工具：Codex。本文件记录实际状态；未来完成一个阶段后更新它，不把计划写成已经实现。

GitHub：[xiaoshecode/thuauth](https://github.com/xiaoshecode/thuauth)，私有仓库。初始工作分支为 `codex/bootstrap`；后续分支和最新提交以 Git 为准。

## 已完成

- 确认用户需求：清华个人账号、明文 JSON、IPv4/IPv6、自动重登、指定网站探测、约 10 MiB/min 可配置下载、多 worker。
- 两个只读子 agent 分别完成协议与运行时设计评审，结论已整合进架构和实施计划。
- 确定 Python 3.12+、asyncio、aiohttp、filelock、标准库配置与日志的方案。
- 项目规则改以 AGENTS.md 为入口；CLAUDE.md 为兼容指引。
- 保存迁移前的规则、提示词和设计文档；原始完整聊天未导出，不能宣称存档包含全部对话。
- 保存 GoAuthing 固定提交的全部 23 个受 Git 跟踪文件，附 SHA-256 清单、LICENSE 和 NOTICE。
- 提供配置示例、Codex 接续提示词和按阶段验收要求。

## 尚未实现

- 没有 pyproject.toml、uv.lock、src/thuauth/ 或应用测试。
- M0–M4 均未完成；没有可运行的 thuauth CLI。
- 没有执行 Python 应用测试、真实校园网登录或公网大流量请求。
- 尚未确定实际 bulk 下载目标；示例中 targets 为空，bulk.enabled 为 false。

## 下一个可执行任务

默认开始 M0，先检查现有文档是否有影响实现的矛盾，再完成以下最小交付：

1. 建立 pyproject.toml、dev 依赖、src layout 和 uv.lock。
2. 实现 Config、必要数据模型与错误类型，配置校验明确报告字段路径。
3. 实现 CLI --help、init、config check；init 在本机交互式读取凭据，不将其写进测试或日志。
4. 补充有意义的配置和 CLI 测试，运行静态检查与测试。
5. 更新本文件，列出真实命令结果与下一阶段任务。

M0 的功能不需要连接校园网。不得为了“完成”而预先生成整套未实现协议和保活函数。

## 待验证的设计问题

- double_stack=1 是否使一次认证同时开通另一栈，以及 auth4/auth6 的状态生效延迟。
- auth6 门户的实际 IP 字段格式；原 Go 正则仅匹配 IPv4 文本。
- 空 IP、challenge 返回 IP、最终签名 IP 的绑定规则；ac_id 自动探测在不同接入网络的行为。
- 主动节流与 aiohttp 读超时、预读缓冲的交互，先用本地服务验证。
- 持续下载源及其 IPv6 支持。此项不阻塞 M0–M2。

## 已执行的检查

已检查示例 JSON、当前文档的相对链接与代码围栏、Git 忽略规则、暂存文件范围，以及新增文档的 whitespace check。23 个参考文件的暂存 Git blob 与上游提交逐个一致，同时保留 SHA-256 清单。暂存范围不含本机凭据、会话配置或位于项目根目录的上游 Actions。

独立只读 reviewer 已复核规则入口、历史边界、M0 可执行性、许可和参考完整性，未发现发布前需要修正的实质矛盾。

Python 应用尚未创建，因此没有运行 pytest、ruff 或 mypy。同步完成状态以实际 git remote、git status 与远端分支为准。

## 下次开场

```text
请读取 AGENTS.md、HANDOFF.md、docs/architecture.md 和 docs/development.md。
核对实际文件与 Git 状态，不依赖之前的聊天记录。
当前目标是 M0：建立可运行包骨架、配置校验和测试。
对阻碍实现的架构矛盾先给出具体修正，再继续施工。
完成后运行检查并更新 HANDOFF.md，报告未验证的行为。
```
