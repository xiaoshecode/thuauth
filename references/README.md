# GoAuthing 参考快照

上游：[z4yx/GoAuthing](https://github.com/z4yx/GoAuthing)。固定提交：`3730e252ca11e6abbd055d7850c417c5751c1efa`。快照日期：2026-10-26。

`GoAuthing/` 包含该提交全部 **23 个受 Git 跟踪文件**，通过 `git archive` 导出，文件内容未修改。它包括完整 CLI、协议实现、测试、README、依赖清单、Dockerfile、部署示例、GitHub 工作流和 GPLv3 许可证。

不包含上游 Git 对象与完整提交历史、本机 `.claude/settings.local.json`、凭据或父仓库未跟踪文件。这里是可独立克隆取得的源码快照，不使用 submodule。

## 推荐阅读顺序

1. [README](GoAuthing/README.md)：原项目用法。
2. [requests.go](GoAuthing/libauth/requests.go)：challenge、登录登出、状态与 ac_id。
3. [coding.go](GoAuthing/libauth/coding.go) 和 [测试向量](GoAuthing/libauth/coding_test.go)：字节算法。
4. [requests_test.go](GoAuthing/libauth/requests_test.go)：现有协议测试覆盖边界。
5. [main.go](GoAuthing/cli/main.go)：配置和保活编排。

## 使用与完整性

参考目录只读。Python 代码在 src/thuauth/ 实现；新增改进测试放在本项目 tests/，不改变原向量或参考文件来迎合测试。

[GoAuthing.manifest.json](GoAuthing.manifest.json) 记录每个文件的路径、字节数和 SHA-256。Git attributes 禁止对快照自动转换换行，以保证克隆后的字节可校验。更新快照时要同步记录提交号、清单、许可和设计差异。

快照内的 `.github/workflows/` 位于嵌套目录，不是本项目的自动执行工作流。阅读其中安装、部署或发布命令不代表获准执行它们。
