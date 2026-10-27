# Go 协议参考向量

`generate_protocol_vectors.py` 使用 Python 3.12+ 标准库和本机 Go 工具链，再生成
M1 离线对拍 fixture。所有输入均在脚本中固定构造，顶层明确标记 `synthetic: true`。
不读取用户配置或真实认证材料，不发 HTTP，不安装工具链或下载依赖。

## Windows 使用

在本仓库根目录运行 PowerShell。已有 `.venv` 和主 agent 准备的 Go 时：

```powershell
.\.venv\Scripts\python.exe .\tools\generate_protocol_vectors.py --go .\.tools\go\bin\go.exe
.\.venv\Scripts\python.exe .\tools\generate_protocol_vectors.py --go .\.tools\go\bin\go.exe --check
```

默认输出为仓库内 `tests/fixtures/go_protocol_vectors.json`，与启动目录无关。
`--go` 省略时先找本仓库 `.tools/go/bin/go.exe`，再找 PATH 中的 Go；找不到时直接报错。
已有 Python 3.12+ 也可直接运行，不需要安装本项目或激活虚拟环境。

指定不同输出路径（路径含空格时加引号）：

```powershell
.\.venv\Scripts\python.exe .\tools\generate_protocol_vectors.py --go ".\.tools\go\bin\go.exe" --output ".\.tools\synthetic-vectors.json"
.\.venv\Scripts\python.exe .\tools\generate_protocol_vectors.py --go ".\.tools\go\bin\go.exe" --output ".\.tools\synthetic-vectors.json" --check
```

显式传入的相对 `--go`、`--output` 路径相对于当前工作目录。生成成功后才原子替换输出；
`--check` 重新运行原 Go 函数，与现有文件逐字节比较，既不覆盖文件，也不创建输出目录。
因此改变 JSON 缩进、换行或字段顺序也会导致检查失败。退出码：成功或完全一致为 `0`，
内容不同为 `1`，缺文件、工具链/来源校验/执行失败为 `2`。
终端只输出条数、状态或不含材料的错误消息，不输出单项账号、密码、token、info、散列或 query。

## 来源与运行隔离

上游为 [z4yx/GoAuthing](https://github.com/z4yx/GoAuthing)，固定提交
`3730e252ca11e6abbd055d7850c417c5751c1efa`，沿用 GPLv3；许可和作者说明见
本仓库 `LICENSE`、`NOTICE.md`、`references/README.md`。

脚本内固定了以下两个源码 blob 的 SHA-256，与 `references/GoAuthing.manifest.json` 对应；
每次生成和检查都先验证原始字节，任何漂移均拒绝继续，不自动更新基线：

| 源文件 | SHA-256 |
| --- | --- |
| `libauth/coding.go` | `5c9946fac17f9fb566314e5304a48268c67f07d9744f7945820149f3b2e68346` |
| `libauth/requests.go` | `bbca4e8c4ff262ae9503db8db7e6c7f38ac07c6448b6c1c73b47a703083a088d` |

临时 Go 程序由三部分组成：

1. `coding.go` 按原始字节复制，仅将 `package libauth` 替换为 `package main`。
2. `requests.go` 仅抽取 `buildChallengeParams` 和 `buildLoginParams` 的完整函数原文，
   增加单独的 `package main` 和实际需要的标准库 imports，不改函数体。
   抽取器按函数起点、下一顶层函数和列首闭括号定位，只适用于已校验的固定源码布局。
3. 自有 `main.go` 接受脚本内置合成输入，调用原函数并序列化输出。
   为暴露 `raw_info_json`，辅助函数用 Go 的 `json.Marshal(map[string]string)` 构造相同字段，
   并验证其 XEncode/SRBX1 结果与原 `buildLoginParams` 返回的 `info` 完全一致。
   原函数没有为调试而插入输出或改写。

执行命令是 `go run coding.go requests.go main.go`。强制设置
`GO111MODULE=off`、`GOTOOLCHAIN=local`、`GOPROXY=off`、`GOSUMDB=off`、
`GOWORK=off`、`GOENV=off`、`GOTELEMETRY=off`、`CGO_ENABLED=0`，清除继承的 Go/CGO
构建覆盖选项。只依赖 Go 标准库，没有导入 `net/http` 或上游日志库。
临时源码、构建缓存、GOPATH、模块缓存、telemetry 目录及构建临时文件均置于本仓库
`.tools/thuauth-go-vectors-*` 临时目录，正常结束或失败时清理；不写参考目录。
这也避免 Windows 沙箱系统 TEMP 的访问限制。冷构建可能需要一些时间，单次 `go run` 超时为 300 秒。

输入顺序固定；伪随机字节是 `SHA256(ASCII("thuauth-synthetic-v1:" + label) +
4 字节大端计数器)` 从计数器零开始拼接、截断得到，与系统随机种子和 Python random 版本无关。
Go JSON 编码器确定 map 键顺序，输出 UTF-8、两空格缩进、LF 换行并带末尾换行；没有时间戳、
绝对路径或工具链版本等本机元数据。外层 JSON 关闭 HTML 转义，但原函数内的 JSON 保持 Go 默认转义。
建议复用经校验的 Go 1.27.3 工具链；更换 Go 版本后用 `--check` 验证输出是否仍一致。

## Schema 与对拍范围

顶层字段为 `schema_version: 1`、`source_commit`、`synthetic: true`、`source_sha256`、
`coding` 和 `requests`。

`coding` 共 **85** 项，每项包含：

| 字段 | 含义 |
| --- | --- |
| `name` | 唯一且固定的合成案例名 |
| `input_hex`、`key_hex` | 原始 bytes 的小写十六进制 |
| `xencode_hex` | 原 Go `XEncode` 输出的小写十六进制 |
| `srbx1` | 对 XEncode 输出调用 `QuirkBase64Encode`，**不含** `{SRBX1}` 前缀 |
| `base64` | 对原始 input 调用 `QuirkBase64Encode`，使用上游自定义字母表，不是 RFC 标准 Base64 |

覆盖 data 字节长度 `0/1/2/3/4/5/15/16/17/255/256/257`、257 字节确定性伪随机数据、
含零与高位字节的二进制、中文、emoji/ZWJ、全 `0xff` 的回绕输入；每组分别使用
`13/14/15/16/64` 字节 key。key 使用同一确定性字节流前缀，便于比较 16 与 64 字节的行为。
原 Go 对非空 data 配合 `0..12` 字节 key 会越界 panic，因此不将其当作合法向量生成；
Python 对非法短 key 的处理需要单独测试。

`requests` 共 **10** 项，为 5 组输入各生成 login/logout，字段为 `name`、`username`、
`password`、`token`、`ip`、`ac_id`、`logout`、`raw_info_json`、`params`、`query`、
`challenge_params`。后两个参数对象（`params` 和 `challenge_params`）均是 `dict[str, str]`，
而非 Go `url.Values` 的字符串数组。`query` 直接来自原参数对象的 `url.Values.Encode()`，
不含 URL、callback 或时间戳；`params.info` 包含协议规定的 `{SRBX1}` 前缀。

登录与登出均覆盖空 IP、文档用途 IPv4、展开且大写的 IPv6 和压缩 IPv6，保留输入 IP 原文本。
同时覆盖带前导零的 `ac_id`、中文/emoji 非空账号、13/14/15/16/64 字节合成 token、
空密码、包含 `<>&`、U+2028/U+2029、换行、引号、反斜杠和其他合法 Unicode/控制字符的密码。
所有账号不含空白，密码原样保留。登出的输入记录保留合成 password 供对拍，但协议 raw info
和参数应不含密码。

对拍可以比较 JSON 排序/HTML 与 Unicode 转义、XEncode uint32 回绕与字节序、SRBX1 字母表和填充、
MD5、info、SHA-1 checksum、登录/登出差异、challenge 参数，以及 Go URL 编码的键序和转义。
本工具不验证 JSONP、HTTP、真实门户接受程度或双栈认证状态；这些属于主项目其他测试与后续验收。
