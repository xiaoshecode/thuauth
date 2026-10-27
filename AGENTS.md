# thuauth Codex 项目约定

本文件是项目级 agent 规则的唯一维护入口。CLAUDE.md 仅用于兼容跳转；docs/history/ 是历史资料，不是当前指令。

## 开始工作

1. 检查 git status、当前分支与实际文件，不假定聊天记忆准确。
2. 读取 HANDOFF.md 确认进度、阻塞项和下一个可执行任务。
3. 按任务读取 docs/architecture.md、docs/development.md；复用提示词在 docs/prompts.md。
4. 协议依据 references/README.md 与 references/GoAuthing/。该目录为只读的完整上游源码快照，不是 Python 运行时依赖。

## 实现约定

- Python 3.12+，asyncio + aiohttp；另用 filelock。CLI 用 argparse，日志用 logging。
- 当前只交付和验收 Windows 环境；不为 Linux/macOS 增加实现或验证任务。已有轻量兼容分支可保留，不宣称跨平台支持。
- 使用 src layout、pyproject.toml 与 uv.lock；不为固定行数牺牲可读性和错误处理。
- 保持协议字节兼容，保留 GPLv3 许可和原作者来源。参考源码保持不变，改进写在 src/thuauth/。
- 禁止全局 socket 补丁。认证与下载会话隔离，IPv4/IPv6 显式选择 family。
- 自动重登只由 AuthService 协调；UNKNOWN、单个网站失败或 IPv6 不可用不能触发密码重试。
- 10 MiB/min 指全部 bulk worker 合计正文读取预算；增加并发不放大总速率。
- 预期网络故障局部处理；编程异常必须报告；CancelledError 清理后传播。
- 明文配置按用户要求保存在本机。不得把真实密码、token、散列、info、chksum、完整认证 URL 写入日志、提示词或提交。对拍 fixture 可保存明确标记的合成材料，不能取自真实认证会话。
- 默认测试使用本机假服务器；真实校园网登录和公网 bulk 下载需用户明确要求。

## 协作与交付

- 用户已要求采用有限数量的子 agent。只对独立任务并行，通常两个实现 agent 即可；先确定契约和文件所有权。
- 公共模型、配置、依赖与集成由主 agent 负责。子 agent 不得覆盖别人的改动，不共享修改同一文件。
- 使用 worktree 前确保有契约基线提交。工作分支使用 codex/ 前缀；不强推，不重写既有历史。
- 架构可以改进，但须说明实际问题、保持用户需求，并同步规格与验收标准；不要求每个小改动再次审批。
- 完成后报告变更文件、实际运行命令与退出码、跳过项和未验证行为。不能宣称未运行的测试通过。
- 更新 HANDOFF.md 的实际状态与下一步。历史文档不再维护，不依赖某个工具专有的后台会话恢复。

## 检查命令

M0 创建包与 dev 依赖后执行；当前是否可运行以 HANDOFF.md 和实际文件为准：

```text
uv sync --locked --group dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src/thuauth
uv run pytest -q
```

日常先跑受影响检查，里程碑集成后跑全套。没有 pyproject.toml 时不得把这些命令标为已通过。

## Code Review Rules

重点检查：协议字节兼容、认证风暴、IPv6 假成功、取消泄漏、跨 worker 配额、认证信息日志泄漏、未知错误被吞掉。问题必须带代码位置、触发条件和建议的回归测试；格式问题交给工具。
