# 项目文档

建议先运行工作台，再沿一次研究流程阅读实现。

| 想了解什么 | 入口 |
| --- | --- |
| 安装、启动、配置和备份 | [开始使用](GETTING_STARTED.md) |
| 模块分层、代码位置与设计取舍 | [架构概览](architecture/overview.md) |
| 事实源、检索范围与快照一致性 | [数据和上下文](architecture/data-context.md) |
| 后台执行、幂等恢复与插件边界 | [执行与恢复](architecture/execution.md) |
| 本地修改与验证方式 | [开发说明](DEVELOPMENT.md) |
| 一次可运行的离线演示 | [演示指南](demo/DEMO_GUIDE.md) |
| 最新实测结果和已知局限 | [项目状态](PROJECT_STATUS.md) |
| 较早阶段的确定性评测记录 | [Phase 6 历史结果](evaluation/phase6-history.md) |

应用名称统一为“证据驱动的智能研究工作台”。Python 项目名 `research-knowledge-core` 保留兼容。

当前目录只保留使用、实现和验证说明。旧版代码与过程材料的历史查阅方式见[开发说明](DEVELOPMENT.md#历史资料)。基准输入位于 [benchmarks](../benchmarks/)，私人运行归档不随仓库分发。
