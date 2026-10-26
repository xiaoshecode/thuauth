# thuauth 项目约定

当前阶段：架构与开发文档已建立，Python 包尚未实现。先检查实际文件与 Git 状态，再继续对应里程碑。

- 行为规格：docs/architecture.md；实施与验收：docs/development.md。
- Python 3.12+，asyncio + aiohttp；运行依赖另含 filelock。CLI 用 argparse，日志用 logging。
- 使用 src layout、pyproject.toml 和 uv.lock；不为了固定行数压缩可读性或删除错误处理。
- 协议依据 GoAuthing 基线 3730e252ca11e6abbd055d7850c417c5751c1efa；保持字节兼容和 GPLv3 来源声明。
- 禁止全局 socket 补丁。认证与下载池隔离，IPv4/IPv6 显式选择 family。
- 只有明确 OFFLINE 才提交自动重登；UNKNOWN、IPv6 不可用与目标网站失败不能触发密码重试。
- 登录、登出统一由 AuthService 协调；凭据错误阻断自动认证，频率限制进入冷却。
- 10 MiB/min 指所有 bulk worker 合计正文读取预算；并发数不放大总预算。
- 预期网络故障局部处理；编程异常不能无限吞掉；CancelledError 必须传播。
- 默认测试仅访问本机假服务器；本任务未授权实际提交校园网凭据或启动公网 bulk 下载。
- 不读取真实凭据用于生成上下文；不得打印密码、token、散列、info、chksum、完整认证 URL。
- 公共接口与依赖文件由主 agent 负责；子 agent 限定写入范围，集成后单独跑测试。
- 不宣称未执行的检查通过；报告命令、退出码、跳过项和未验证行为。

M0 建立工程后使用：

```text
uv sync --locked --group dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src/thuauth
uv run pytest -q
```

当前下一步为 M0。架构边界需要变更时，先说明理由并同步行为文档，避免 agent 各自修改约定。
