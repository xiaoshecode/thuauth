# Codex 接续与实施提示词

这些提示词用于新会话。长期规则以 [AGENTS.md](../AGENTS.md) 为准，实际进度以 [HANDOFF.md](../HANDOFF.md) 为准。它们不依赖某个模型名、effort 等级或专有 Workflow 插件。

M0–M4 已实现。下面的 M0/M1/M2 模板保留作为验收范围记录，不应把它们误当成待办；正常接续优先用“维护与新功能”。

## 维护与新功能

```text
请读取 AGENTS.md、HANDOFF.md、docs/runtime-contract.md，核对 git status 和实际代码。
当前只交付 Windows，先复现本次需求涉及的问题，再修改最小必要模块。
协议、重试、双栈、限速或取消行为的变化需有本机假服务器/确定性回归。
只对可独立验收的任务使用最多两个子 agent，并给出互不重叠的可写文件范围。
不读取真实配置提交到聊天，不连接真实认证或启动公网 bulk。
执行真实检查、更新 HANDOFF.md、提交并同步 GitHub，报告结果与未验证项。
```

## 恢复项目上下文

```text
请读取 AGENTS.md、HANDOFF.md、README.md，检查 git status 和已有代码。
按需阅读 docs/architecture.md、docs/development.md 和 references/README.md。
简要说明实际已完成内容、尚未实现内容及下一个可执行任务。
不要把计划、历史提示词或子 agent 自评当成已运行验证。
```

## 继续审查架构

```text
只审查并改进架构，不开始实现应用。
读取 AGENTS.md、HANDOFF.md、docs/architecture.md、docs/development.md。
核对 references/GoAuthing 中的实际协议，重点查双栈、状态误判、重试、取消和限速。
最多调用两个只读子 agent，分别负责协议与运行时；主会话负责整合。
只采纳有具体失败场景或源码依据的建议，避免为个人工具引入插件框架。
更新设计文档和 HANDOFF.md，区分源码事实、设计选择和必须校内实测的行为。
```

## 开始 M0 施工

M0 已完成；此模板只用于回顾验收范围或重建。

```text
请按 AGENTS.md、HANDOFF.md 和 docs/development.md 完成 M0。
先检查工作区与架构契约，建立 pyproject、src layout、依赖锁文件。
实现配置模型、严格校验，以及 CLI --help、init、config check。
不要提前扩展完整认证或保活模块，不创建大量 NotImplementedError 占位代码。
配置错误、CLI 返回码和凭据不泄漏需要测试；不连接真实校园网。
执行实际检查，报告命令与退出码，并更新 HANDOFF.md。
```

## 继续 M1 协议

M1 已完成；此模板只用于复查范围。

```text
读取 AGENTS.md、HANDOFF.md 和 docs/development.md，核对已实现的 M0 契约。
完成 M1：移植 references/GoAuthing 的纯编码、登录登出参数与 JSONP 解析。
保留 GPLv3 来源；参考文件只读。补齐 Go/Python 完整报文对拍和畸形响应测试。
网络、双栈在线检查与真实校园网登录留给后续阶段，不提前写保活占位代码。
执行实际检查，更新 HANDOFF.md，报告哪些行为仍需校内验证。
```

## 继续 M2 Windows 认证客户端

```text
读取 AGENTS.md、HANDOFF.md、docs/architecture.md 和 docs/development.md，核对工作区。
当前只支持 Windows，沿用 M0 配置与 M1 纯协议接口完成 M2。
实现地址族明确的 HTTP 会话、AuthService、login/logout/status/doctor；用本机假服务器测试。
重点覆盖双栈联动、UNKNOWN 不重登、账号阻断、认证超时后复查、取消与资源释放。
保留协议与 HTTP 层分离，不记录敏感认证材料，不连接真实认证服务或启动公网 bulk。
运行真实检查，更新 HANDOFF.md，报告 Windows 验证结果与校内待测项。
```

## 按文件范围并行实施

M2/M3 已实现，后续按实际维护任务评估是否可以独立并行，不为并行而重复实现模块。

```text
先检查 HANDOFF.md 中的阶段与依赖是否满足。
公共模型和 HTTP 接口必须已有可审查契约，再调用两个实现子 agent。
协议 agent 只修改 protocol/ 与协议测试；流量 agent 只修改 limiter.py、traffic.py 及对应测试。
主会话负责配置、依赖、认证协调和集成；不得由多个 agent 同时修改共享文件。
若任务依赖未满足，先完成必要阶段，不强行并行。
每个 agent 提交修改文件、测试命令、结果与未验证项。
主会话审查和运行集成检查后再汇总完成状态。
```

## 独立评审代码

```text
只读审查当前需求与最终 diff，重点检查实际故障路径。
检查协议字节兼容、重复登录、IPv6 假成功、配额倍增、任务无法退出和敏感日志。
每条结论提供文件位置、触发条件和建议的回归测试；区分缺陷与偏好。
不要自动修改文件，不把格式建议升级为功能缺陷，不连接真实认证服务。
```

## 结束阶段并交接

```text
核对实际变更与已运行检查，更新 HANDOFF.md。
记录完成范围、命令和退出码、跳过测试及原因、遗留问题与下一步。
检查参考快照未改动，真实配置和诊断日志未被暂存。
提供简短交接说明；不要把尚未执行的校内实测写为通过。
```

迁移前的 Claude 提示词原样保存在 [历史目录](history/README.md)。Codex 规则入口依据 [官方 AGENTS.md 说明](https://developers.openai.com/codex/guides/agents-md/)。
