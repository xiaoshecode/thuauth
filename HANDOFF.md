# thuauth 当前进度与接续

更新日期：2026-10-27。主要开发工具：Codex；当前只交付 Windows。

GitHub：[xiaoshecode/thuauth](https://github.com/xiaoshecode/thuauth)，本轮实际 API 查询为 **公开仓库**，已纠正旧文档的“私有”描述，未修改可见性。工作分支 `codex/windows-complete`，基于 M1 `d94edf5`，运行契约基线 `2cf18f1`；默认分支为 `codex/bootstrap`。最终同步情况以 Git 和文末记录为准。

## 当前状态

M0–M4 的 Windows 代码、文档及 CI 配置已完成，本机最终全量检查和远端 Windows 双版本 CI 均通过，两个实现 agent 的交叉评审已结束并修复确认问题。不能将此状态等同于真实校园网认证成功。

- M0：Python 包、uv.lock、严格不可变配置、init/config check，Windows 写入前 ACL、原子发布和不覆盖保护。
- M1：纯协议编码、参数/JSONP/分类，85 个 Go 编码向量、10 个完整请求案例，合成材料可离线再生成。
- M2：http.py 显式 AF_INET/AF_INET6、独立认证/流量会话、TLS、直连、有界读取、无重定向/认证 GET 重放；auth.py 账号级协调、阻断/冷却、双栈复查；login/logout/status 可运行。
- M3：limiter.py 全局 FIFO 正文预算；traffic.py probe、bulk、Range、目标冷却、统计；daemon.py 健康/流量/统计生命周期、休眠间隔恢复；Windows 用户级进程锁。
- M4：doctor 的 DNS/门户/TLS/目标小请求诊断、CLI 覆盖和退出码、滚动脱敏日志、Windows CI、README、校内验收清单及维护提示词。

原有提示词/设计历史与 23 个 Go 参考文件完整保留；所有参考 SHA-256 已重新核对。未读取用户真实配置，未使用真实凭据或执行公网 bulk。

## 重要实现契约

1. 认证协议保持固定 Go 基线的字节行为，MD5 不是 HMAC；token 原文、Go JSON、空签名 IP 语义不擅改。challenge 返回 IP 不自动覆盖签名 IP。
2. 状态查询与 ac_id 发现解耦。在线后发现页可能已恢复正常内容；只读 status 直接查门户，未配置 ac_id 时用空字符串，解析失败为 UNKNOWN。登录/登出仍重新发现有效 ac_id 和 challenge，失败不猜数字。
3. 只有明确 `not_online` 才是 OFFLINE；未知/超时/DNS 失败不提交密码。真实标志和门户格式待校内确认。IPv6 不回落 IPv4 假报成功。
4. 每次认证后复查两栈，包括响应丢失场景；凭据/账号拒绝阻断本进程后续认证，429 类认证频率问题账号级冷却。候选地址族轮换，避免 IPv4 持续失败使 IPv6 饥饿。
5. online 门控要求配置账号匹配且观测未过期。bulk 全 worker 合计默认 10 MiB/min，probe/认证另计；bytes 包含读取后判定无效的正文，successes 单独记录，不是网卡级计量。
6. bulk 默认关闭，用户自行配置下载源。probe 默认 info/百度/Bing。Range/200 均有读取上限，实际 read ≤16 KiB；可识别 HTML/跳转/空响应不算成功，HTML 检查限 Content-Type/前 512 字节。服务器冷却跨栈共享，传输故障按目标＋family 隔离；Retry-After 超过 24 小时暂停目标。
7. 普通故障局部退避；编程错误、关键任务意外返回或意外取消报告并停止。停止先取消并等待子任务，再关闭池；Ctrl+C 不登出。休眠间隔恢复保留账号阻断和目标冷却，丢弃积压预算。
8. login/logout/keep 共用默认用户配置目录下的 runtime.lock，与 -c 无关；status/doctor 只读，不持写锁。日志/JSON 不输出真实账号、密码、token、签名或完整 URL，异常只保留类型/代码位置。
9. CLI --no-ipv6、keep --no-auto-relogin/--rate/--workers-ipv4/--workers-ipv6/--duration 等覆盖只影响本次运行，重新校验后使用，不改 JSON。
10. doctor 对所有已配置 probe/bulk 目标的启用地址族各至多一次 1 KiB 请求，即使 bulk 未开启。它不认证，也不能证明目标适合长期下载。未全部在线或诊断未通过时返回 4，账号阻断返回 3。

精确接口见 [docs/runtime-contract.md](docs/runtime-contract.md)，使用方法见 [README.md](README.md)。

## 实际本机验收

环境：Windows、Python 3.12.14、uv 0.13.5、Go 1.27.3 windows/amd64。本地 uv 为 `.tools/bin/uv.exe`，使用 `UV_CACHE_DIR=.uv-cache`；Python 为 `.venv/Scripts/python.exe`。普通 pytest 不需要 Go。

| 实际命令/检查 | 退出码 | 结果 |
| --- | --- | --- |
| `uv sync --locked --group dev` | 0 | 24 个包与锁文件一致 |
| `.venv/Scripts/ruff.exe check .` | 0 | 通过 |
| `.venv/Scripts/ruff.exe format --check .` | 0 | 43 文件通过 |
| `.venv/Scripts/python.exe -m mypy src/thuauth` | 0 | 17 个源文件 strict 检查通过 |
| `.venv/Scripts/python.exe -m pytest -q -ra` | 0 | **850 passed，1 skipped，22.37 秒** |
| `python tools/generate_protocol_vectors.py --go .tools/go/bin/go.exe --check` | 0 | Go 重新生成 85 编码/10 请求案例，与 fixture 一致 |
| `uv build --offline` | 0 | wheel 与 sdist 构建成功 |
| 分发包成员、参考清单检查 | 0 | wheel 23 项、sdist 39 项，无工具/缓存/真实配置；23 参考文件 SHA 一致 |
| 独立 wheel 环境安装及 `python -I -m thuauth keep --help`、`config check`、网络模块 import | 0 | 来自 `.tools/wheel-smoke/Lib/site-packages`，无需源码编辑安装 |
| `git diff --check` | 0 | 通过 |
| GitHub Windows / Python 3.12.10 | 0 | lint、format、mypy、build；**850 passed，1 skipped，39.51 秒**；Go 对拍通过 |
| GitHub Windows / Python 3.14.9 | 0 | lint、format、mypy、build；**850 passed，1 skipped，36.36 秒** |

唯一 skip 是 `test_posix_private_permissions`，Windows 下不适用。IPv6 回环 HTTP 与流量测试均实际执行，没有 IPv6 跳过；Windows ACL、两个真实进程互斥、进程被终止后锁恢复均测试通过。

独立安装首次 `uv pip install --offline` 因缓存缺依赖失败，随后用 uv.lock 导出固定运行依赖安装成功；未改变锁文件。构建提示缓存位于源码树内，已检查最终包成员确认不包含缓存。

本轮两个实现 agent 分别拥有 HTTP/诊断、流量/限速；主 agent 负责共享契约、认证/daemon/app/CLI、测试与交付。独立评审发现 IPv4 饥饿和任务意外取消静默停止，新增两项回归先得到 **2 failed**，修复后在上述全量检查中通过。旧冷却测试的尝试顺序按轮换行为更新，未删除失败场景。

流量交叉评审另外复现同网址单栈 DNS 失败中止另一栈、带前置注释的 HTML 被计为成功，并验证大 read 扩大 aiohttp 缓冲水位；4 项新回归先失败，修复后通过。主会话同时验证并修复 traffic 内部任务意外返回/取消导致静默失效，2 项回归先失败、随后通过。最终计数如上，未把中途失败隐去或当作最终结果。

## 未验证项

- 没有执行真实校园网登录、登出或公网持续下载。实际 `not_online` 标志、ac_id 发现、空 ac_id 查询、IPv6 门户 HTML、challenge IP 绑定、auth4/auth6 联动、校内 DNS/TLS 均待验证。
- 真正的断网/休眠/系统 Ctrl+C 行为和交互 getpass 仍需用户在 Windows 终端验证；已覆盖合成时间跳跃、取消、资源释放与锁，但不冒充系统级实测。
- 持续下载源由用户选择，默认 bulk.enabled=false；未验证任何公网源的长期可用性或最高配置速率。
- DNS 等待有超时，但系统 getaddrinfo 已开始的解析线程不能强制终止，极端系统解析器卡死仍可能延长 Python 进程退出。
- 本机实测 Python 3.12.14，最终远端实测 Windows/Python 3.12.10 与 3.14.9。其他 Python 版本没有实测；Linux/macOS 不在交付范围。

## 后续工作

按 [docs/campus-validation.md](docs/campus-validation.md) 由用户在校园网执行验收，凭据只在自己的终端输入。若门户行为与保守解析不同，先提取最小脱敏字段结构、建立合成回归，再修改对应协议/HTTP 模块；不要直接放宽 UNKNOWN 为 OFFLINE。

新会话读取 AGENTS.md、本文件及 docs/prompts.md 的“维护与新功能”。M0–M4 均已有实现，不重复施工，不依赖 Claude 专用命令或历史会话恢复。

## 收尾与远端记录

完整实现提交为 `423f983`，最终评审修复为 `544d6b6f16d4231c37c1c10e41c73da687728036`。该最终代码的 [Windows CI 40438208804](https://github.com/xiaoshecode/thuauth/actions/runs/40438208804) 状态 success，两项作业与上述计数均从实际日志核对。

本文件的最终结果补记不改代码、测试或工作流，因此作为单独文档提交，不重复运行 CI。工作分支 `codex/windows-complete` 与默认分支 `codex/bootstrap` 同步相同内容，均只快进、不强推；文档收尾提交号以 `git log -1` 为准。未创建发布标签或上传 GitHub Release；可安装 wheel/sdist 位于本机忽略目录 dist/，克隆后可用 uv build 重建。
