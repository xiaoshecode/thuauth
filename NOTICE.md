# 来源与修改说明

本仓库包含 GoAuthing 的完整源码快照，并以它为 Python 移植的参考。

- 原项目：GoAuthing，https://github.com/z4yx/GoAuthing
- 上游提交：3730e252ca11e6abbd055d7850c417c5751c1efa
- 参考位置：references/GoAuthing/
- 原作者与贡献者：保留上游 README、源码和 Git 项目中的声明；本仓库不将其归为自己的原创代码。
- 上游许可证：GNU General Public License version 3，完整文本见根目录 LICENSE 与 references/GoAuthing/LICENSE。

2026-10-26：导入上述提交的 23 个源文件，内容不修改；新增 thuauth 架构、实施计划、Codex 项目约定、提示词、交接文件和来源清单。随后新增 M0 Python 包、本地配置校验与 CLI 及对应测试。

2026-10-27：在 src/thuauth/protocol/ 中移植 libauth/coding.go 的字节编码，以及 requests.go 的 challenge、登录和登出参数构造；保留 Go 的 uint32、JSON 转义与签名行为。新增有界 JSONP 解析、输入检查和基于 messages.go 部分错误码的安全分类；这些边界检查是 Python 实现的改进。新增从原 Go 函数再生成合成 fixture 的工具与对拍测试。参考快照不修改；本阶段仍不发起真实认证或保活流量。

2026-10-27：M2–M4 在 http.py 中参照 libauth/requests.go 实现发现、状态查询、challenge、登录和登出传输。新增显式双栈连接池、保守状态分类、有界解析、禁止认证重放与重定向；新增账号级认证协调、保活、流量预算、诊断、Windows 生命周期和本地测试。现代传输与故障处理是 Python 版本的改进；实际学校兼容性待校内验证，参考快照未改动。

本仓库按随附 GPLv3 许可发布；后续移植应保留来源并标明修改。新建仓库或翻译为 Python 不改变原代码适用的许可条件。许可证文本中 Free Software Foundation 的版权声明属于许可证文本，不是 GoAuthing 源码的作者声明。
