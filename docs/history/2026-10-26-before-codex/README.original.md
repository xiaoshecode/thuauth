# thuauth

个人使用的清华校园网认证与保活工具，计划采用 Python 实现，支持本地 JSON 凭据、IPv4 与 IPv6、自动重登、低频站点访问以及可配置的持续下载流量。

当前交付的是架构与开发约定，尚未实现 Python 程序，也未进行校园网认证实测。

- [架构与行为规格](docs/architecture.md)：模块、状态、配置、流量口径和故障处理。
- [实施计划与多 agent 协作](docs/development.md)：开发阶段、验收测试和 Claude Code 使用方法。
- [Claude 项目约定](CLAUDE.md)：后续编码会话的精简上下文。

下一步从开发计划 M0 开始建立可测试的包骨架。运行命令和配置文件格式在实现前均属于设计规格。

协议实现将参考 [GoAuthing](https://github.com/z4yx/GoAuthing)，基线提交为 `3730e252ca11e6abbd055d7850c417c5751c1efa`。移植代码与测试须保留来源和适用的 GPLv3 许可；发布前加入完整 LICENSE 和来源说明。
