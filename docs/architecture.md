# thuauth Python 架构

版本：2026-10-27，供实现与审查使用。当前只交付与验收 Windows；Linux/macOS 不在本阶段支持范围，已有轻量兼容代码不构成跨平台承诺。

thuauth 是个人使用的清华校园网客户端。它用学号与密码认证，分别管理 IPv4、IPv6 的在线状态，并在认证有效时访问指定网站、生成可配置的下载流量。实现采用 Python 3.12+、asyncio 和 aiohttp；业务状态、协议编码和 HTTP 传输分离，便于独立测试与替换。

## 需求与边界

- 真实学号、密码保存在本机 JSON；交互输入使用 getpass，Windows 默认路径为 `%APPDATA%/thuauth/config.json`。当前不扩展其他系统支持。
- 支持 login、logout、status、keep；补充 init、config check、doctor 供初始化和排错。
- 默认启用双栈与自动重登；缺少 IPv6 网络时，IPv4 继续工作并明确显示 IPv6 不可用。
- 低频访问 info.tsinghua.edu.cn、baidu.com、bing.com，并允许自行增删目标。
- bulk 支持多个并发下载任务，默认目标量为所有 bulk worker 合计 10 MiB/min，可调整。这个数值不包括低频 probe 与认证请求，它们分别记录。
- 使用下载产生请求上行和响应下行；这不等于上下行各有 10 MiB/min。需要等量上传时，应另外增加支持上传的自有测试端点。
- 不承诺持续流量一定阻止学校侧下线；周期性认证检查与自动恢复仍然必要。
- 首版不做 GUI、后台服务安装、热更新配置、插件加载、消息总线或数据库。CLI 之外的界面将来通过应用层接入。

## 技术选择

| 部分 | 选择 | 原因 |
| --- | --- | --- |
| Python | 最低 3.12，Windows 稳定版解释器 | 使用成熟接口，后续 CI 在 Windows 验证 |
| HTTP | aiohttp | 原生异步流式读取、连接池、IPv4/IPv6 family 配置 |
| 并发 | asyncio.TaskGroup | 统一任务生命周期与取消，减少跨线程共享状态 |
| 跨进程互斥 | filelock | 复用跨平台文件锁，避免自行维护 Windows 锁代码 |
| 配置 | json、dataclasses、显式字段校验 | 少量配置不引入动态配置框架 |
| CLI 和日志 | argparse、logging | 标准库即可满足需求 |
| 工程 | pyproject.toml、uv.lock、src layout | 依赖可重现，避免从仓库根目录误导入包 |
| 开发检查 | pytest、pytest-asyncio、ruff、mypy | 行为测试、异步测试、格式与类型检查 |

直接运行依赖为 aiohttp 和 filelock；不把它们的传递依赖忽略成“总共两个包”。实现时锁定经过验证的版本，不依赖 alpha 接口。

aiohttp 的 TCPConnector 提供 family 参数，因此不使用全局 socket.getaddrinfo 补丁。参考 [aiohttp 客户端接口](https://docs.aiohttp.org/en/stable/client_reference.html)。TaskGroup 与取消行为以 [Python 3.12 文档](https://docs.python.org/3.12/library/asyncio-task.html) 为兼容基线，不调用后续版本才增加的方法。

## 目录与职责

以下结构同时列出当前实现与后续计划。实际完成状态见 HANDOFF.md；M0 已实现 config、models、errors、cli 和包入口；M1 已实现 protocol/coding、protocol/srun 纯函数与合成对拍。HTTP 会话、认证协调和保活模块尚未实现。

```text
thuauth/
├── pyproject.toml
├── uv.lock
├── README.md
├── LICENSE
├── NOTICE.md
├── CLAUDE.md
├── AGENTS.md
├── HANDOFF.md
├── config.example.json
├── docs/
│   ├── architecture.md
│   ├── development.md
│   ├── prompts.md
│   └── history/
├── references/
│   ├── README.md
│   ├── GoAuthing.manifest.json
│   └── GoAuthing/       # 固定提交的完整只读源码快照
├── src/thuauth/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py           # 参数、交互、终端输出、退出码
│   ├── app.py           # 构建依赖、打开资源、分发应用操作
│   ├── config.py        # 配置模型、校验、读写与迁移
│   ├── models.py        # 状态、观察结果、操作报告
│   ├── errors.py        # 有限的异常类型与错误分类
│   ├── http.py          # 双栈会话、超时、连接池与有界读取
│   ├── auth.py          # 在线状态、认证协调、重试与冷却
│   ├── daemon.py        # 保活生命周期和健康巡检
│   ├── traffic.py       # probe、bulk worker、目标冷却与统计
│   ├── limiter.py       # 共用字节预算，可注入时钟
│   ├── logging_setup.py # 日志格式、滚动文件、脱敏
│   └── protocol/
│       ├── __init__.py
│       ├── coding.py    # 纯字节算法与散列
│       └── srun.py      # 参数构造、JSONP 与响应解释（纯函数）
└── tests/
    ├── fixtures/        # 合成或脱敏的协议响应
    ├── test_coding.py
    ├── test_protocol.py
    ├── test_go_vectors.py
    ├── test_config.py
    ├── test_auth.py
    ├── test_http.py
    ├── test_traffic.py
    ├── test_limiter.py
    └── test_cli.py
```

依赖方向为 `cli → app → auth/daemon/traffic → protocol/http`，纯算法与数据模型位于下层。http 不导入认证业务；protocol 不导入 CLI 或守护循环；daemon 不解析 JSONP。

不为每个小类创建文件。traffic 初期容纳 probe、bulk 与统计，只有复杂度增长后才拆包。依赖在 app 显式构造，无全局 Session、可变配置或隐式单例。

M0 的 CLI 直接调用本地配置模块；app 和异步资源生命周期在 M2 按需引入，避免为当前几个本地命令增加空编排层。尚不创建认证/状态机的占位类。Credentials 和 Target 为当前实际共用的数据模型。

## 核心接口

只在稳定边界使用类型接口，初版不预制通用插件框架。以下异步客户端接口属于 M2 及以后；M1 只实现纯协议函数。

```python
# protocol/coding.py：编码边界明确使用 bytes
def xencode(data: bytes, key: bytes) -> bytes: ...
def srbx1_encode(data: bytes) -> str: ...


# 返回状态观测而非 bool，网络失败不能被折叠成离线
class SrunClient:
    async def discover(self) -> AccessContext: ...
    async def status(self, access: AccessContext) -> OnlineObservation: ...
    async def login(self, credentials: Credentials, access: AccessContext) -> AuthReply: ...
    async def logout(self, username: str, access: AccessContext) -> AuthReply: ...


class AuthService:
    async def status_all(self) -> StatusReport: ...
    async def ensure_online(self, family: IPFamily) -> AuthOutcome: ...
    async def logout_all(self) -> LogoutReport: ...


class TrafficRunner:
    async def run(self) -> None: ...
    def snapshot(self) -> TrafficSnapshot: ...


class Daemon:
    async def run(self, stop: asyncio.Event) -> None: ...
```

Config、Credentials、AccessContext 和报告对象使用 dataclass；配置尽量 frozen、slots，集合使用 tuple。Credentials.password 设 repr=False。OnlineObservation 包含 family、状态、观测时间、用户名、IP、原因；不把密码或原始认证响应放入报告。

SrunClient 注入由 app 管理的 HTTP 会话，而非自行无限创建会话。业务测试替换客户端对象；HTTP 测试用本地服务覆盖实际传输边界。协议类不负责等待退避。

## 双栈认证与状态

每个地址族分别维护观察结果和恢复进度：

| 状态 | 含义 | 自动行为 |
| --- | --- | --- |
| ONLINE | 认证接口确认当前目标账号在线 | 启用该栈的正常流量任务 |
| OFFLINE | 认证接口明确返回离线 | 通过唯一认证入口尝试重登 |
| UNKNOWN | 请求超时、DNS 失败、响应无法识别 | 冷却后再查询，不直接提交密码 |
| UNAVAILABLE | 没有该栈的可用地址或路由 | 低频重新检查，另一栈继续 |
| AUTHENTICATING | 正在执行认证过程 | 合并或等待同一认证操作 |
| BLOCKED | 密码错误、账号禁用或账号冲突 | 停止相关自动认证，给出可操作提示 |

双栈指分别检查与管理 auth4、auth6，不预设两侧会话完全独立。流程为检查两栈、在账号级认证锁下补齐离线栈、每次操作后复查。学校的 double_stack 行为可能让一次认证影响另一栈，必须实测；不能无条件同时重复登录。没有 AAAA 记录只说明某个目标不支持 IPv6，不能单凭这一结果把本机 IPv6 标为 UNAVAILABLE，也不能悄悄回落 IPv4 后报告 IPv6 成功。

每次实际重登前重新获取接入信息与 challenge，不复用旧 token/IP。ac_id 可自动发现，也允许显式覆盖；发现失败不得随意回退到某个数字。发现接口实际走哪个地址族，以原实现和校园网验证为依据。

AuthService 是唯一提交登录/登出的入口。health、启动流程、手动命令均调用它；probe 和 bulk 不提交凭据。获取认证锁后再次核对状态，避免两个调用者依次重复登录。等待锁的调用者取消不能影响持锁操作；如果以后共享登录 Task，则由服务管理它，等待者取消与服务停止必须分别处理。错误示例：

- E2553 等凭据问题进入 BLOCKED，同账号两栈共享阻断，防止交替尝试错误密码。
- E2532/E2533 等频率限制按账号冷却，退避可取 30、60、120 秒直至上限 600 秒，并加随机抖动。
- E2620 不能直接视为目标账号成功，重新核对在线账号和 IP。
- 登录响应超时可能意味着服务端已经处理，先查状态再决定是否重试。
- 未知协议错误只输出本地定义的分类和诊断，不透传原始错误码或消息；限制重试，不能统一当作网络抖动。

在线却属于其他账号时，不主动踢下该账号。status、doctor 不要求输入密码，也不执行隐式登录。keep 的 Ctrl+C 只结束守护，不主动登出；logout 是显式命令。

## HTTP 资源与任务生命周期

为认证和流量分别建立 IPv4、IPv6 会话，共四类连接池。分别指定 AF_INET、AF_INET6，保留域名完成 TLS 与 SNI 校验，不能用 IP 替换域名来绕过 family 配置。下载占满连接不能阻塞状态查询。

默认禁用环境代理继承，并明确报告直连模式，避免系统代理把“本机校园网认证”送到另一出口。认证与流量 Session 不共用认证 Cookie 或 header。TLS 校验默认开启，不自动降级 HTTP，也不使用 verify=False 等价做法。接入发现若确需沿用原项目的 HTTP 门户探测，作为单独且不带凭据的明确步骤，不影响认证端点的 HTTPS 要求。

```text
app 创建并持有会话与实例锁
  └─ daemon 的 TaskGroup
      ├─ IPv4 健康巡检
      ├─ IPv6 健康巡检
      ├─ probe 调度
      ├─ bulk workers
      └─ 周期统计
```

停止事件只表达停止意图；TaskGroup 正常退出不会自动取消无限循环。daemon 持有任务引用，收到 stop 后显式 cancel 并 await 所有任务，再退出会话上下文。CancelledError 必须传播，清理放在 finally。CLI 使用 asyncio.run，Windows Ctrl+C 不依赖仅在 Unix 可用的 add_signal_handler。

预期的请求失败在最小任务范围内处理并冷却；未知编程异常向主控传播，打印脱敏 traceback 并退出非零，避免进程还在但关键任务早已死亡。任务意外正常返回也需要主控识别，不允许健康巡检悄悄停止。

认证请求设置连接、读取与整体期限；bulk 单独设置连接期限、实际读等待期限和单次传输期限。限速等待不算服务器读取超时，不能把认证的 5 秒超时直接用于低速大文件下载。实现需用慢服务器测试验证 aiohttp 缓冲与超时的实际交互。

检测到较长调度间隔或网络恢复时，重新查询状态、丢弃失效连接、DNS 缓存与过期接入信息，跳过错过的巡检周期，不补发积压请求。使用递增的网络版本标记异步结果，旧版本响应不得覆盖新状态；恢复时重置下载额度，但保留凭据阻断与服务端冷却。无需首版就实现各操作系统专用的网络事件监听。

## 流量模型

probe 每 20 秒选择一个站点、每次最多读取 32 KiB，并记录响应码与重定向结果。三个默认目标约每分钟轮询一遍。可按目标指定 ipv4、ipv6 或 both；both 会分别记录结果，不把不支持 IPv6 的站点当作校园网掉线。

bulk 面向用户选择的大文件或可控测试服务，默认下载目标留空。配置目标后显式启用；未配置时仍可运行认证与 probe。info、百度、Bing 只做低频访问，不用大量重复抓取小页面凑配额。公共镜像站不预设为无限循环下载源，使用前确认目标可用于持续测试。

配额使用无歧义单位 `download_mib_per_minute`：10 MiB/min = 174,762.67 B/s ≈ 170.67 KiB/s。这是全部 bulk 任务合计的应用响应体长期平均目标上限，允许有界小突发；不是任意滚动 60 秒的严格硬配额。服务器限速、目标故障、暂停认证等会使实际值更低。

- IPv4、IPv6 worker 共用全局预算；两栈可用时公平调度，默认至少各一个 worker。改变 worker 数不改变总速率。
- 请求前控制并发与请求间隔，读块前申请字节预算，读取后按实际字节结算；短读退回未使用额度，等待与取消不泄漏预算。
- 限速器使用单调时钟，明确最大突发量。避免锁内 sleep；测试公平性、取消、唤醒以及并发总额。
- 支持 Range 的源请求有限区间并检查 Content-Range；忽略 Range 返回 200 时仍执行本地读取上限，不整文件读入内存。
- 使用小块流式读取，不落盘大文件；设置 Accept-Encoding: identity 并关闭自动解压，以响应体字节统计，减少压缩口径歧义。
- 明确这不是网卡计量：TLS、TCP、重传、HTTP 头以及系统/库预读不计入应用正文统计。限制缓冲大小和并发可约束偏差，但不能保证网卡每分钟恰好 10 MiB。
- 302/登录页、小 HTML 响应、零字节内容不能算有效 bulk 进展；bulk 默认关闭自动重定向，只允许显式受信目标和有限跳数，未知跳转暂停该源。错误状态限量读取后关闭。
- 429 遵守 Retry-After；异常大值将目标暂停并提示，不提前重试。冷却按目标跨两栈、所有 worker 共享。404/403、连续失败等同样使目标进入冷却。全部目标失效后等待，不立即循环，也不通过提高并发补足“欠下”的流量。
- 网络 UNKNOWN 时暂停该栈 bulk；普通目标失败不改变认证状态。probe 与健康检查各自保留低频恢复路径。

首版可以只做一个全局 byte limiter，按地址族分配 worker；只有公平性测试显示某栈长期饥饿时才增加额外调度结构。

## 配置规格

下面是设计示例，不包含真实凭据或可直接压测的下载地址。启用 bulk 后必须填写可用 targets。

```json
{
  "schema_version": 1,
  "username": "",
  "password": "",
  "auth": {
    "ipv4": true,
    "ipv6": true,
    "auto_relogin": true,
    "health_interval_seconds": 30,
    "ac_id_ipv4": null,
    "ac_id_ipv6": null
  },
  "probe": {
    "interval_seconds": 20,
    "max_response_bytes": 32768,
    "targets": [
      {"url": "https://info.tsinghua.edu.cn/", "family": "both"},
      {"url": "https://www.baidu.com/", "family": "ipv4"},
      {"url": "https://www.bing.com/", "family": "ipv4"}
    ]
  },
  "bulk": {
    "enabled": false,
    "download_mib_per_minute": 10,
    "workers_ipv4": 1,
    "workers_ipv6": 1,
    "chunk_bytes": 16384,
    "max_response_bytes": 1048576,
    "targets": []
  },
  "logging": {
    "level": "INFO",
    "file": null
  }
}
```

bulk target 结构为 `{"url": "https://your-host/path/file.bin", "family": "ipv4"}`；地址仅示意，不作为内置默认值。同一域名经验证支持双栈时可配置两条。

加载顺序为内置默认值、指定 JSON、用户显式传入的 CLI 覆盖值。CLI 布尔选项要能覆盖 true 为 false，不能用逻辑或合并。嵌套对象按已知模型解析，数组整体替换；不做任意字典递归合并。

拒绝未知键、错误类型、非有限数值、重复 JSON 键、负数、过大并发与非法 URL，并报告字段路径。缺失可选字段使用默认值；凭据校验按命令区分。新增可选字段保持旧配置可读，破坏性变更通过 schema_version 显式迁移，未知新版本报错。

init 交互式创建配置，keep 在无配置或非交互环境中直接给出明确错误，不在后台等待输入。写入使用同目录临时文件和原子替换；Unix 限制为用户可读写，Windows 放在用户目录并检查适用 ACL，不宣称 chmod(0600) 能建立 Windows ACL。真实配置默认位于仓库外。

M0 已明确的行为如下：

- `config check` 默认只检查结构，允许尚未填写凭据的示例；`--require-credentials` 额外要求非空账号密码，不验证学校侧是否接受。
- `init` 要求 stdin、stderr 均为终端；使用 getpass 两次确认密码，无法隐藏输入时中止。只清理学号首尾空白，密码的空格和 Unicode 原样保存。
- 默认不覆盖已有文件；以同目录临时文件加原子硬链接完成首次发布，避免并发创建时误覆盖。不支持硬链接的文件系统明确报错。`init --force` 用原子替换重置全部选项到默认值并保存新凭据，拒绝目标符号链接。
- Windows 对空临时文件调用系统 whoami/icacls 设置当前用户的私有 ACL，成功后才写入凭据；Unix 临时文件模式为 0600。不会修改已有父目录权限。
- `-c/--config` 可放在根命令或子命令后；M0 没有业务布尔覆盖选项。JSON 中显式 false 与空列表不会被默认值覆盖。
- 配置最多 1 MiB，接受 UTF-8 BOM；错误包含已知字段路径或 JSON 行列，未知字段名统一显示 `<unknown>`，不回显原文与非法值。重复键、非有限数值、超深嵌套会报配置错误。命令行用法错误同样不回显错误参数。
- 临时文件清理失败返回脱敏错误，保留原始写入失败原因；若配置已经发布则明确说明已写入，并提示检查配置目录内的 `.thuauth-*.tmp`。文件在写入任何凭据前已限制权限。
- schema_version 仅接受整数 1；auth 至少启用一栈；ac_id 为正数字符串或 null。health 间隔为 1–3600 秒，probe 间隔为 1–86400 秒。
- probe 响应上限 1–1048576 字节；bulk 的块上限 1–1048576 字节且不超过单次响应上限，单次响应最大 1 GiB。总 worker 数不超过 32；下载目标速率为 0.01–1048576 MiB/min。
- 各类目标最多 64 项，仅允许不含内嵌账号密码或 fragment 的 HTTP(S) URL。bulk 开启时，每个有效 worker 地址族都需要相应目标；禁用地址族的 worker 配置保留但不激活。probe 空目标列表表示不探测。

上述数值上限用于约束无效配置，不表示程序已具备对应流量能力。认证与流量行为仍需在 M1–M3 实现和验证。

## 实例互斥与诊断

单个用户环境的 keep、login、logout 共用稳定路径的进程锁，与配置文件路径无关；否则换一个 -c 就会绕过互斥。实例在任何有副作用的网络请求之前取锁，持有到会话关闭；崩溃后残留锁文件不等于锁仍被持有。keep 持锁期间，独立 login/logout 给出提示，避免刚登出又被守护进程登录。status 和只读 doctor 不争用写锁。

“多开”优先通过一个进程内多个 worker 实现。首版不支持同账号多个守护进程竞争认证。filelock 参考 [官方文档](https://py-filelock.readthedocs.io/en/latest/)。

日志包括时间、模块、task 名称、family、事件、目标主机、耗时、字节与错误类型；常态每分钟输出汇总，状态切换即时输出。统计区分 bulk/probe、实际读取/有效数据，并记录观测时间与降级原因。

即使 --debug，也不记录 password、MD5(password)、token、info、chksum、cookie、完整认证 URL 或原始认证响应。HTTP 异常往往包含请求 URL，应在异常边界结构化提取安全字段，不直接把异常字符串传给日志。用户名默认遮掩；不自动打包原始配置进诊断文件。

doctor 只检查配置、DNS、地址族、TLS、认证接口可达性、目标基本行为，输出每项通过/失败/未测试，不偷偷提交登录或启动大流量。status --json 提供稳定的机器可读输出。

退出码建议：0 操作成功，1 非预期程序错误，2 配置或用法错误，3 明确认证拒绝，4 临时网络/协议失败或查询离线，5 实例占用，130 用户中断。部分成功/降级情况写进结构化结果；status 两栈均在线才返回 0，单栈运行需显式禁用不需要的栈才能符合这一成功定义。

## 协议兼容与发布

M1 的实际协议边界如下；这些函数不发起网络请求：

- `coding.xencode(bytes, bytes)` 与 `srbx1_encode(bytes)` 实现字节编码。非空输入的 key 至少 13 字节，才能形成 Go 使用的四个小端 word；过短 key 报 ProtocolError，替代参考实现的数组越界。空数据编码为空。长 token 按参考实现取前四个 word 参与编码，但 checksum 仍使用完整 token。
- `srun.serialize_info` 复现 Go 字典键排序、紧凑 JSON、UTF-8，以及 `<>&`、U+2028、U+2029 转义。登录包含 password；登出省略该字段。
- `build_challenge_params`、`build_login_params` 与 `build_logout_params` 返回参数映射，不加入 callback、不拼接 URL、不记录日志。登出 API 不接收密码。纯编码支持合成空密码以验证 Go 兼容；实际登录前由 Config/AuthService 要求填写凭据，不能把空字符串当成登出。HTTP 层在 M2 负责实际请求编码。
- 登录摘要为普通 MD5(password)，不是 HMAC；`{MD5}` 前缀只出现在 password 参数，checksum 使用无前缀摘要。登出 checksum 省略密码摘要。
- IP 可以为空，或为有效 IPv4/IPv6 文本；不规范化其拼写、不附加 scope ID、不自动用 challenge 的地址替换签名地址。ac_id 保留有效数字字符串原文。
- `parse_jsonp` 限制响应最多 64 KiB，严格匹配 callback；允许外围 ASCII 空白和单个尾分号。拒绝无效 UTF-8、重复键、非有限数值、超深嵌套、非对象根、额外脚本和截断响应，不执行任何 JavaScript。
- `parse_challenge` 要求 res=ok 和有效 challenge，保留可选 client_ip 为只读证据；token 与地址不进入 repr。
- `parse_auth_reply` 返回固定类别：ACCEPTED、BLOCKED、THROTTLED、ALREADY_ONLINE、REJECTED。凭据错误、禁用、地址绑定/黑名单、欠费和额度耗尽等已映射账号限制为 BLOCKED，E2532/E2533 为 THROTTLED，E2620 要复查。error=ok 时 ecode 只允许缺失、整数 0 或字符串 "0"，其他组合报 ProtocolError。ACCEPTED 仅表示接口接受操作，仍需 M2 核对在线状态。未知返回文本和错误码不向上游回显，不能自动当作可重试的离线状态。
- M1 只提交标记 synthetic 的合成对拍材料。Go fixture 工具从只读快照提取原函数执行，不通过 Python 实现产生自己的期望答案。

现代化改变 Python 组织方式与故障处理，不能自行把服务器要求的 MD5、SHA1、XEncode 或 JSONP 换成另一套认证协议。以仓库 `3730e252ca11e6abbd055d7850c417c5751c1efa` 为移植基线，并用测试固定字节行为。

移植时必须锁定以下差异：

- Go 按 UTF-8 字节装入小端 uint32，运算溢出回绕；Python 必须显式使用 bytes、括号与 32 位掩码，不能把字符长度当字节长度。
- challenge 字符串直接转字节作为 key，不是十六进制解码后的 key；短或畸形 challenge 应报协议错误，不能数组越界。
- 此仓库的密码摘要是普通 MD5(password)，不是其他学校常见实现中的 HMAC-MD5；外层使用 `{MD5}` 前缀，checksum 中使用不带前缀的摘要。
- info 内部字段为 acid，外层参数为 ac_id；登录和登出的密码字段及 checksum 组成不同。
- 回归测试固定 Go 的 JSON 序列化字节，包括键排序、紧凑格式、Unicode 及 HTML 特殊字符转义。这里追求与基线对拍一致，不推断服务器只能接受一种 JSON 键顺序。
- JSONP 按明确 callback 解析，不执行返回内容；是否容忍外围空白及尾分号需通过测试明确，畸形 JSON 不标记离线。
- 原状态页面 IP 提取仅匹配数字和点，不能原样用于 IPv6。应解析并用 ipaddress 验证返回地址，IPv6 页面格式需校内确认。
- 原实现没有采用 challenge 响应中的 IP；新实现区分请求 IP、服务器报告 IP 与最终用于签名的 IP。未验证绑定规则前不能擅自改变空 IP 语义。

原 Go 测试主要覆盖编码和 JSONP 包装，登录参数测试被注释。因此“平移原测试”不足以证明整个登录流程正确，必须补充固定凭据的完整参数对拍和失败响应测试。

GoAuthing 的协议代码和测试是 GPLv3 项目的一部分。完整快照已保存在 references/GoAuthing/，来源和文件清单见 references/README.md。移植时标注原作者、仓库、基线与修改，发布仓库保留 LICENSE 和适用的 GPL 条款；换语言、换目录或新 git init 不会消除来源许可。参考 [GNU 关于代码翻译的说明](https://www.gnu.org/licenses/gpl-faq.en.html#TranslateCode)。

单元测试通过只能验证实现一致性，真实 auth4/auth6 联动、ac_id 探测和校园网下线恢复需在校内另行验证。
