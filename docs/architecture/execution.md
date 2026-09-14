# 执行与恢复

## 从请求到后台执行

FastAPI 保存命令及队列意图，独立 Worker 执行 PDF 处理、报告、研究运行和投影等长任务。统一研究入口 `POST /api/v1/research-commands` 使用 `Idempotency-Key`：相同键和请求返回原命令，相同键配不同请求返回冲突。

命令保留已创建的任务、快照、报告或运行 ID，失败后能够针对原目标恢复。命令只是编排记录，业务状态仍由对应资源保存。`project_run` 必须明确指定项目和已有或新建任务，不自动选择项目。

## 队列与业务存储

[JobRepository](../../app/runtime/job_repository.py)负责通用入队、领取、续租、完成/失败及Worker心跳，通过`KnowledgeRepository.jobs`共享同一个SQLiteDatabase。业务层的`enqueue_job_tx`调用传入现有事务连接，使资源创建与入队共同提交或回滚。

[ReportRepository](../../app/knowledge/report_repository.py)负责报告的创建、派发、重试、进度更新和最终提交，通过`KnowledgeRepository.reports`访问。报告结果与job终态在同一事务提交，并在提交前复核请求、证据来源版本和执行权；模型调用仍在报告服务中。

[KnowledgeRepository](../../app/knowledge/repository.py)负责入库、审核发布与投影outbox，并统一协调过期任务和业务资源的恢复。[KnowledgeCoreRepository](../../app/knowledge/core_repository.py)负责来源版本、证据与正式知识的同步和回读，不承担Worker调度。所有组件共用SQLite事实库，数据库结构及历史任务格式不变。

## 租约与旧执行者隔离

Worker 领取任务时记录 owner、attempt 和 lease，并定期续租。业务写入和最终提交在事务中校验 `job_id + attempt + lease_owner`。租约被回收后，即便旧模型调用晚返回，旧执行者也不能覆盖当前结果。

过期任务由 Worker 恢复，API 重启不接管现有执行权。默认并发为一；当前验证没有给出高并发吞吐结论。

## AgentRun 与检查点

每次 AgentRun 固定项目、任务、快照 ID/哈希、插件版本、工作流及执行预算。启动工作流前，Worker 在执行权保护下核对快照中的项目与任务修订号；不一致时标记 `stale_context`，要求新快照和新运行。

LangGraph 按运行和工作流命名空间保存检查点。恢复沿用原工作流版本，不能因为默认策略更新而改变已排队运行的含义。工具调用保留序号、权限、参数摘要、结果和幂等信息。

最终结果由平台校验并原子保存 Output、Artifact 和可选 MemoryProposal。检查点用于恢复，不能代替正式产物。需要保留的中间研究内容由平台管理并通过引用传递，避免把完整草稿塞进检查点。

## 研究与反馈

研究工作流从固定快照取证，校验结构和引用；不同版本提供各自有界的修复或复检行为。仍不合格时进入 `needs_review` 或失败状态，不以正常产物冒充成功。

用户反馈可以修订任务并发起新的研究，旧快照、旧报告和生成尝试继续保留。人工编辑稿被接受，不会自动证明其对应生成原件正确。

`research` 与 `research_v2` 至 `research_v7` 等版本由[研究插件](../../app/domain_plugins/research/plugin.py)注册。候选版本不是默认效果提升的承诺；保留它们也用于既有运行兼容和实验追溯。

## 插件和页面边界

静态注册表包含 Research 和 Game Modeling。插件声明版本、工作流、命名空间、预算、工具权限和产物类型，通过窄 runtime port 构建工作流，返回最终提交命令；平台持有持久化和检查点权限。

Game Modeling 在限定的 Formula/Patch 证据上做确定性计算，补丁版本不符时拒绝提交。它没有另一套事实库或后台执行器。

React 通过受限 API 视图展示知识、任务、上下文、运行、产物、反馈和实验。运维 API 提供健康状态、任务历史和针对性恢复；页面操作不能跳过审核、插件版本或租约检查。React 运行页面还提供投影重建与原始状态查看，启动方式见[本地工作台](../GETTING_STARTED.md#本地工作台)。

## 投影重建任务

网页经显式确认和幂等键创建 `projection_rebuild` 任务，复用现有 `knowledge_jobs`，无需额外业务表。请求只排队；Worker 续租并从已审核事实重建投影，在各适配器调用前和最终提交时检查执行权。结果计数与完成状态一起持久化，失败可针对原任务重试。

同一时刻只接受一个排队或运行中的网页重建。外部调用期间不持有新增长数据库事务；租约丢失会阻止后续步骤与过时结果提交，但已发出的外部操作不能被撤回，各投影也没有跨服务原子性。维护时应停止其他写入，失败后按相同范围重建恢复。

新任务需要当前 `unified-worker-v2`。升级时先停止旧 Worker，旧研究工作流和检查点不变。已有 CLI 仍可独立执行维护，不能与网页重建并行使用。

## 验证入口

[运行测试](../../tests/test_agent_runtime.py)、[可靠执行测试](../../tests/test_reliability.py)、[反馈测试](../../tests/test_run_feedback.py)及[领域插件测试](../../tests/test_domain_plugins.py)覆盖恢复、提交与权限边界。[离线演示](../demo/DEMO_GUIDE.md)通过临时数据库调用现有生产服务链路，不访问正式数据或真实模型。
