# thuauth

使用 Codex 协作开发的个人清华校园网认证与保活工具，计划采用 Python 3.12+、asyncio 和 aiohttp。支持本地 JSON 凭据、IPv4/IPv6、自动重登、指定网站访问及可配置下载流量。

**当前阶段：设计与参考材料已就绪，尚未实现 Python 应用。** 下次可以继续审查架构，或直接从 M0 建立可运行骨架。实际进度以 [HANDOFF.md](HANDOFF.md) 为准。

## 从 Codex 继续

将此仓库克隆到本机，直接在 Codex 中打开 **thuauth 仓库根目录**，不要把 references/GoAuthing 当成工作项目。

私有仓库地址：[xiaoshecode/thuauth](https://github.com/xiaoshecode/thuauth)。使用已授权的 GitHub 账号克隆即可取得设计、提示词与完整参考源码。

打开项目后输入：

```text
请读取 AGENTS.md、HANDOFF.md 和 docs/prompts.md，核对实际工作区。
沿用已确认需求，先处理阻碍实现的架构问题，再完成 M0。
按 docs/development.md 运行真实检查，更新 HANDOFF.md 后报告结果。
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
| [config.example.json](config.example.json) | 无凭据的配置规格示例 |
| [references/README.md](references/README.md) | 上游来源、固定提交和文件完整性 |
| [references/GoAuthing/](references/GoAuthing/) | 完整 Go 参考源码、测试、README、部署示例 |
| [docs/history/README.md](docs/history/README.md) | 迁移前提示词与设计的原样存档 |

## 功能约定

- 学号、密码由用户保存在本机 JSON，不放入仓库。
- 双栈分别观察、按需认证；查询失败不会直接触发重登。
- probe 低频访问 info、百度、Bing，也允许配置其他站点。
- bulk 目标为所有下载 worker 合计约 10 MiB/min，可配置速率、并发和目标；具体下载源待选择。
- “多开”通过单进程内多个 worker 实现，避免多个进程争抢认证。

## 许可和参考源码

仓库附有 [GPLv3 许可证](LICENSE) 和 [来源说明](NOTICE.md)。参考快照来自 [z4yx/GoAuthing](https://github.com/z4yx/GoAuthing) 的 `3730e252ca11e6abbd055d7850c417c5751c1efa`，保留原文件内容与许可。

快照不是 Git submodule，克隆本仓库即可获得全部参考材料。快照里的 GitHub Actions 位于 references/ 下，不会作为本项目的工作流自动运行。Python 实现放在未来的 src/thuauth/，不直接修改参考快照。
