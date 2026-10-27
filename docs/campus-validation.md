# Windows 校园网验收

以下是尚待用户执行的步骤，**不代表已经通过**。自动测试只用合成凭据和本机 IPv4/IPv6 服务。密码只在自己的终端或配置文件输入。

## 首次验证

1. 在校园网络运行 `uv run thuauth init`、`uv run thuauth config check --require-credentials`，确认密码输入隐藏、配置路径正确。
2. 运行 `doctor --json`、`status --json`，记录两栈 DNS/TLS/门户分类、状态、退出码。doctor 对 probe/bulk 配置目标各执行有界小请求，不启动下载循环。
3. ac_id 发现失败时，只使用确认过的接入点值覆盖配置。当前只接受明确 `not_online` 标志作为离线；真实格式不同需先脱敏、建立合成 fixture 再适配，不把解析失败视为离线。
4. 运行 `login` 后再查 status，确认双栈分别在线，记录一次认证是否联动另一栈。重复 login 应只复查，不重复提交。
5. `keep --duration 120` 默认只启用 probe，观察 info、百度、Bing 的结果；网站失败不应导致密码重试。不要故意用错误密码触发学校账号限制。

以上命令均使用 `uv run thuauth` 前缀。ac_id、IPv6 门户 HTML、签名 IP 绑定、真实离线标志和 double_stack 联动均需实测。

## 故障恢复与退出

1. keep 运行时，另一个终端的 status/doctor 应可读，login/logout/另一个 keep 返回 5。换配置路径不能绕过锁。
2. 暂时断网再恢复：断网时 UNKNOWN/UNAVAILABLE、对应 bulk 暂停；恢复后先查状态，明确离线才重登。
3. 休眠再恢复：应重新建立连接、复查状态，不补发积压请求/额度。自动测试模拟时间跳跃，不能代替真实系统休眠。
4. Ctrl+C 应有界退出且不登出，新进程可以持锁。停止 keep 后显式 logout 并复查；登出仍需要有效接入信息。

## 自选下载源

1. 填写自己选择的文件服务，核对地址族，先 config check、doctor，再启用 bulk。
2. `keep --bulk --rate 10 --duration 300 --json`，以两栈 `traffic.bulk` 的 bytes 合计除以实际运行时间核对长期正文读取速率。启动突发、认证等待、失败冷却影响短测值。
3. 保持 rate 不变、增加 worker 后再测，总预算不应翻倍。
4. HTML、403、404、429、重定向等导致目标不可用时修复或更换源，不靠提高并发凑量。

记录 Windows/Python 版本、网络接入方式、命令、时间、退出码、两栈状态及脱敏统计。不要保存原始认证 URL、响应、Cookie、token、info、checksum 或真实配置；用合成字段值建立问题复现。
