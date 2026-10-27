# M2–M4 运行接口

当前只支持 Windows。共享模型、配置、异常和集成由主 agent 维护。

- `models.IPFamily` 为 ipv4/ipv6；AccessContext 保存 family、ac_id、签名 ip（默认空，不能自动用 challenge IP 替换）。OnlineObservation 保存 family、OnlineState、username、ip、reason、observed_at。
- `http.SessionPool(families)` 是异步上下文管理器，公开 `.auth`、`.traffic` 两个按 family 索引的字典。会话都强制地址族、TLS 校验、trust_env=False；流量池使用 DummyCookieJar、禁用自动解压。`reset()` 关闭旧池并原地更新字典，`close()` 释放资源。
- `http.SrunClient(family, session_provider, *, ac_id=None, base_url=None, discovery_url=None, timeout_seconds=5)` 接收返回当前认证 Session 的可调用对象。默认使用学校 HTTPS 端点；测试可以注入本地服务器。
- SrunClient 的异步接口为 `discover() -> AccessContext`、`status(access) -> OnlineObservation`、`login(credentials, access) -> AuthReply`、`logout(username, access) -> AuthReply`、`diagnose() -> dict[str, str]`。取消直接传播；预期网络错误为 NetworkError，畸形数据为 ProtocolError。方法不自动重试登录。
- `auth.AuthService(config, clients)` 独占认证协调，`status_all()`、`ensure_all()`、`logout_all()` 返回 tuple[OnlineObservation, ...]；`is_online(family)` 是同步流量门控。`invalidate()` 同步使旧观察失效，旧请求结果不得覆盖新状态。
- `traffic.TrafficRunner(config, sessions, is_online)` 接收流量 Session 字典和同步在线谓词。`run()` 长期运行，取消后释放所有任务；`snapshot() -> dict[str, object]` 只含安全汇总；`reset()` 重置积压配额，不清除服务端冷却。不能调用认证或把目标失败写成认证离线。
- traffic 内的所有 bulk worker 共用一个限速器；daemon 只负责健康巡检、长间隔恢复、统计与停止。进程锁由 app 在任何 login/logout/keep 网络请求前取得，并一直持有到资源关闭。

测试可用 `http_server` fixture 创建 aiohttp.web.Application 的本机 IPv4/IPv6 服务；不访问真实学校门户或公网持续下载源。
