# 离线演示

这个演示通过临时数据库和模型替身，展示证据引用、领域执行和产物追溯。它不需要模型密钥，不访问正式数据库、Qdrant、Neo4j 或网络；也不衡量真实模型回答质量。

## 运行

先按[开始使用](../GETTING_STARTED.md)安装依赖，然后从仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe scripts/run_phase6_demo.py --output data/reports/phase6
```

每次执行创建独立结果目录，包含 `run.json`、`report.md` 和 `demo-summary.md`。输出目录不要指向日常业务数据。打开 `report.md` 检查三个案例结果与隔离状态。

## 阅读结果

| 案例 | 观察内容 | 对应实现 |
| --- | --- | --- |
| 研究报告 | 报告引用属于当前证据集合，校验后形成产物 | Context Builder、研究工作流、平台最终提交 |
| 游戏建模 | 限定证据上的确定性公式计算 | 领域插件和共享运行时 |
| 产物追溯 | Artifact 与运行、快照之间的来源关系一致 | 工作台数据视图 |

如需检查范围拒绝、补丁版本不匹配、恢复与幂等等更多边界，运行完整确定性集合：

```powershell
.\.venv\Scripts\python.exe -m app.benchmarking --manifest benchmarks/phase6/cases/v1/manifest.json --output data/reports/phase6
```

基准使用历史版本标识，详见[评测说明](../../benchmarks/phase6/README.md)。结果表示这次确定性契约检查是否通过，不是人工批准的质量基线。以前的运行记录单独保存在[历史结果说明](../evaluation/phase6-history.md)。

## 体验实际工作台

用[本地启动命令](../GETTING_STARTED.md#本地工作台)打开 React 工作台。真实研究需要配置模型与相应服务：导入资料、审核知识、指定项目范围、发起研究，随后核对报告引用和反馈后的新运行。

离线演示生成的是结果文件，不会自动把案例灌入日常工作台。全新克隆也不包含私人实验历史；最新工程验证、真实模型对照及待人工核验项见[项目状态](../PROJECT_STATUS.md)。
