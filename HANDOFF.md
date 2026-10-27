# thuauth 当前进度与接续

更新日期：2026-10-27。主要开发工具：Codex。用户现阶段只需要 **Windows**；实现、测试和后续 CI 均以 Windows 为交付范围，Linux/macOS 不作为当前验收项。

GitHub：[xiaoshecode/thuauth](https://github.com/xiaoshecode/thuauth)，私有仓库。本阶段分支 `codex/m1`，基于 M0 提交 `3dee11f`；最新提交与远端同步情况以 Git 为准。

## 当前状态：M0、M1 已完成

M0 已建立 Python 3.12+ 包、uv.lock、不可变配置和本地 CLI：`--help`、`--version`、`init`、`config check`。明文凭据仅保存本机；Windows 在写入前设置当前用户 ACL，默认不覆盖已有文件，`--force` 原子替换。`config check --require-credentials` 额外检查凭据非空。这些行为本轮全量回归通过。

M1 新增：

- `protocol/coding.py`：Go 兼容 XEncode 与自定义 Base64，显式 bytes、uint32 回绕、小端布局及长度尾字。
- `protocol/srun.py`：challenge/登录/登出参数、Go JSON 序列化、MD5/info/checksum；有界 JSONP 解析、challenge 解析及认证响应分类。所有函数无网络和日志副作用。
- `models.py`：不可变 Challenge、AuthReply 与 AuthReplyKind；`errors.py`：固定分类、脱敏的 ProtocolError。
- `tools/generate_protocol_vectors.py`：从固定 Go 源码提取原函数，用本机 Go 标准库离线再生成 fixture；校验两个源码 SHA-256，不改参考文件、不下载依赖。
- `tests/fixtures/go_protocol_vectors.json`：标记 synthetic 的 85 个编码向量、10 个完整请求案例，包含登录/登出、空 IP、IPv4、IPv6、Unicode、特殊转义、不同 token 长度及合成空密码。
- 编码测试、协议故障路径测试和 Go 对拍测试；普通 pytest 直接读取提交的 fixture，不要求安装 Go。

本轮两个实现子 agent 分别负责编码与 Go fixture 工具，主 agent 负责共享模型、请求/响应协议、集成和文档；第三个 agent 在编码 agent 结束后做独立只读评审。未同时多人修改同一实现文件。

原有提示词、设计与 Go 参考快照均保留；23 个参考文件 SHA-256 全部一致。历史目录不再维护，原始完整聊天未导出，不宣称包含全部对话。

## 已明确的实现契约

1. CLI 当前仍是 M0 本地命令；没有添加不可用的 login/logout/status/keep/doctor 占位命令。M1 的函数供 M2 接入。
2. 登录参数保持原 Go 的字节行为：token 直接编码、不做 hex 解码；MD5 不是 HMAC；Go 键排序、紧凑 JSON、HTML 和 U+2028/U+2029 转义一致。
3. 非空 XEncode 输入要求 key 至少 13 字节，形成四个小端 word；过短输入返回 ProtocolError，替代 Go 越界 panic。空数据返回空 bytes。编码只用前 16 key 字节，checksum 仍用完整 token。
4. 登录纯编码允许合成空密码以验证兼容；实际发起登录前必须调用 Config.require_credentials。空字符串仍为登录，None 只在内部表示登出；公开登出 API 不接收密码。
5. IP 允许空或有效 IPv4/IPv6，保留原文本，不做自动规范化。challenge 的 client_ip 仅记录为证据，不覆盖签名 IP；真实绑定规则仍需校内验证。
6. JSONP 最多 64 KiB，严格匹配 callback；允许外围 ASCII 空白及单个尾分号。拒绝重复键、无效 UTF-8、非有限数值、非对象根、额外脚本和无法解析的嵌套数据。异常不回显原始响应。
7. AuthReply 分类为 ACCEPTED/BLOCKED/THROTTLED/ALREADY_ONLINE/REJECTED。error=ok 时，ecode 只允许缺失、整数 0 或字符串 "0"；异常或冲突组合报 ProtocolError。ACCEPTED 不是在线证明，M2 必须复查。
8. 已映射账号限制（凭据错误、禁用、绑定/黑名单、欠费、额度耗尽）为 BLOCKED；E2532/E2533 为 THROTTLED，E2620 要复查。未映射的拒绝仅返回固定 REJECTED，原始错误码/消息不透传，不据此自动重登。
9. 当前只支持 Windows。已有轻量 POSIX 配置兼容分支和跳过的权限测试保留，但不新增跨平台任务或承诺。

## 实际检查结果

环境：Windows、Python 3.12.14、uv 0.13.5、Go 1.27.3 windows/amd64。uv 位于 `.tools/bin/uv.exe`，Go 位于 `.tools/go/bin/go.exe`；均在 Git 忽略目录，不修改系统 PATH。Go 从官方分发下载并核对官方 SHA-256。运行以下 uv 命令时设置 `UV_CACHE_DIR=.uv-cache`，实际使用项目内 exe 路径。

| 命令或检查 | 退出码 | 结果 |
| --- | --- | --- |
| `uv sync --locked --group dev` | 0 | 24 个包与锁文件一致 |
| `uv run --locked ruff check .` | 0 | 静态检查通过 |
| `uv run --locked ruff format --check .` | 0 | 格式检查通过 |
| `uv run --locked mypy src/thuauth` | 0 | 9 个源文件 strict 类型检查通过 |
| `uv run --locked pytest -q -ra` | 0 | **508 passed，1 skipped** |
| `.venv/Scripts/python.exe tools/generate_protocol_vectors.py --go .tools/go/bin/go.exe` | 0 | 原 Go 函数生成 85 个编码、10 个请求案例 |
| 同一生成命令增加 `--check` | 0 | 重新运行 Go 后 fixture 逐字节一致 |
| `uv build --offline` | 0 | wheel 与 sdist 构建成功 |
| 分发包成员及 fixture 内容检查 | 0 | wheel 15 项、sdist 22 项；协议模块、示例、测试 fixture 与许可齐备，无本地缓存/配置 |
| 参考 SHA-256 清单核对 | 0 | 23 个文件全部匹配 |

370 项 M1 测试包含 167 项编码测试、107 项协议测试、96 项 Go fixture 元数据/对拍测试；加上 M0 的 138 项通过测试，合计 508。唯一 skip 是 `test_posix_private_permissions`，当前 Windows 无法验证 POSIX 权限，且不属于本轮支持范围。Windows ACL 已真实调用系统工具验证。

独立评审指出两项响应分类问题：成功标志与异常 ecode 冲突，以及部分明确账号限制被归为未知拒绝。补充回归先得到 22 failed、7 passed，修复后协议测试 107 项通过，最终全量结果如上。Go/Python 对拍无字节差异。检查中的导入/格式告警已修正；早期结果不代替最终检查。

构建产物仍为 `dist/thuauth-0.1.0-py3-none-any.whl` 与 `dist/thuauth-0.1.0.tar.gz`，版本尚未正式发布。工具提示源码树内存在 uv 缓存；已实际核对分发包成员，缓存未被打包。普通源码包包含可直接用于 pytest 的 fixture；再生成 fixture 请使用保留 references/ 和 tools/ 的完整 Git 仓库。

## 尚未实现与未验证

- M2–M4 尚未完成：没有 HTTP 会话、AuthService、在线状态检查、自动重登、probe/bulk、共享限速器、守护循环、进程锁、诊断命令或 CI。
- 未进行真实校园网认证或公网 bulk 请求。合成对拍证明与指定 Go 基线一致，不能证明当前学校门户接受，也不证明 IPv6 网络在线。
- 未选定持续下载源，bulk.enabled 仍为 false。info、百度、Bing 是未来低频探测目标，不自动作为持续下载源。
- getpass 尚未由用户在实际 Windows 交互终端手动验证；已有模拟交互测试。未输入或读取真实账号密码。
- Windows 的真实 auth4/auth6 联动、ac_id 自动发现、challenge IP 绑定、认证响应完整格式、休眠/断网恢复、下载限速与 Ctrl+C 仍需后续本地服务测试和用户明确授权的校内实测。
- 只验证了上述 Python 与 Go 版本；其他版本、其他系统不在当前交付验证范围。Go 工具链只用于维护 fixture，不是 Python 程序运行依赖。

## 下一步：M2 Windows 认证客户端

1. 引入 app 显式构建与关闭 aiohttp 会话；认证与下载隔离，IPv4/IPv6 connector 明确 family，不使用全局 socket 补丁。纯 protocol 层继续无 I/O。
2. 用本机假服务器实现并验证发现、challenge、登录、登出、状态；保留请求 IP、返回 IP、签名 IP 的来源，错误响应为 UNKNOWN，不能当作确定离线。
3. AuthService 统一认证协调：逐栈检查、串行补齐、操作后复查，凭据错误在账号级阻断，频率限制冷却，超时后先复查，保护在线的其他账号。
4. 接入 login/logout/status/doctor CLI；发起登录前 require_credentials。补齐 Windows 取消、超时、资源释放与地址族不可用测试；IPv6 回环不可用时明确跳过对应项。
5. 运行全量检查并更新本文件。不要扩展保活流量占位代码，不连接真实门户提交凭据；M3 再实现持续流量。

```text
请读取 AGENTS.md、HANDOFF.md 和 docs/prompts.md，核对实际工作区与 Git 状态。
当前只支持 Windows，沿用 M0 配置和 M1 纯协议契约，按 docs/development.md 完成 M2。
用本机假服务器验证 HTTP、双栈认证协调、错误分类、取消与资源释放，不使用真实凭据。
运行真实检查，更新 HANDOFF.md，报告结果与校内未验证项。
```
