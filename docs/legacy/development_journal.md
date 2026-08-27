# Development Journal

本日志持续记录项目从需求、实现、验证到产品使用反馈的完整链路，作为后续技术复盘、项目介绍和简历材料的事实依据。架构的稳定结论写入 `architecture.md`，阶段规划写入 `development_roadmap.md`，这里保留每次关键决策、现场数据、验证证据和待改进项。

## 记录模板

- 日期与阶段
- 用户场景与问题
- 相关模块和调用链
- 现场数据与验证方法
- 结论与产品含义
- 已知风险和后续动作
- 可复用的项目成果表述

## 2026-08-27：阶段五——统一证据、来源身份与投影重建

### 问题与架构决策

- 旧报告检索把 SQLite 生成的 Paper 白名单交给 Qdrant 后，仍直接使用向量 payload 中的标题、正文和页码；payload 漂移理论上可能进入报告。现在 Qdrant 只返回 Chunk 候选 ID 和分数，SQLite 重新加载 Source、Document、Chunk 并验证授权、内容哈希、Source 版本和页码后才形成 `ReportEvidence`。
- 新增 Knowledge Core shadow read，显式呈现 backfill 状态、兼容发布层/Core Paper 差异和授权后的 Core Paper；只有 backfill ready 且两侧无差异时才使用 Core 授权，差异时安全回退兼容层。
- 新增 schema v18 `ingestion_documents`，将 Document 从单一 `ingestion_id` 归属扩展为向前兼容的多对多成员关系。同一规范化 URI 和内容版本可进入多个 Collection，内容变化则得到新的 Document ID，既有 Document 行不被 `REPLACE`。
- Qdrant、Neo4j、Vault 获得从 SQLite 正式事实重建的统一服务和 CLI；Neo4j 按完整受管图重建，避免局部 Collection 修复误删跨范围端点。
- 报告质量门新增检索相关性和来源多样性；当候选池存在至少三篇相关论文时，最终证据必须覆盖至少三篇。150 页继续由配置控制，不固化成不可调整的边界契约。

### 修改范围与验证

- 增加 SQLite 证据重水化、Core shadow diagnostics、来源 URI/版本化 Document ID、受控字符清洗、PDF 标题页眉过滤、三类投影重建和报告 v2 质量指标；React 报告页展示相关性与来源多样性。
- 报告 Worker 在领取历史或恢复任务后重新检查真实 LLM 配置，在任何检索或生成前 fail closed；report/job 终态保持一致。
- 定向回归覆盖未知/篡改 Chunk 拒绝、三来源质量门、跨 Collection 同版本 Document、来源内容版本变化、全量/局部投影重建和重建范围保护。
- 阶段收口时后端全量回归为 177 passed、3 skipped；前端为 35 passed，production build 通过。Ruff、pip check、Compose 配置与 Git diff 检查通过。

### 已知边界与下一步

- `published_*` 兼容表继续双写，本阶段没有物理删表；正式删除必须等待 shadow read 在真实数据上持续无差异。
- Vault 重建覆盖正式受管笔记并保留“人工笔记”区，不主动删除无法确认所有权的历史文件。
- 下一阶段补齐 AgentRun `stale_context`、`needs_review` 处置、明确 Task 选择、React Collection 移动和 Streamlit 运维收口。

### 可复用的项目成果表述

- 将报告检索从“信任向量 payload”升级为“向量只召回、SQLite 证据重水化”，并以来源版本化、多 Collection 文档成员关系和可重建投影形成统一的本地证据信任链。

## 2026-08-21：阶段四——统一研究指令入口

### 问题与架构决策

- 旧 Dashboard 的“开始研究”只创建快速报告；项目研究仍要求用户手工完成 Project、Task、ContextSnapshot、AgentRun 多步操作，且浏览器的两次请求可能在断网后留下重复或孤立资源。
- 新增 schema v17 `research_commands`：存储请求哈希、模式、编排阶段、已创建资源 ID、目标路由与错误。`Idempotency-Key` 只以 SHA-256 保存；相同 key+请求返回原 Command，不同请求复用 key 返回冲突。
- 两种模式共用一个入口但保持产物边界明确：`quick_report` 默认创建证据报告；`project_run` 必须显式选择 Project，并显式复用 Task 或创建 Task，随后由 Worker 创建不可变 ContextSnapshot 和 AgentRun。
- 编排由统一 Worker 执行。Task、Snapshot、Report、AgentRun 使用 Command 派生的稳定 ID；失败保留已创建资源并从失败阶段恢复，不因双击、断网重放或 Worker 重启复制目标。

### 修改范围与验证

- 新增 ResearchCommand model/repository/service、迁移 17、三个 v1 API、Worker job kind 和 Runtime 投影；Context、Task、Report、AgentRun 创建路径增加可选确定性 ID，但不改变既有调用者的随机 ID 默认行为。
- React Dashboard 替换旧快速报告卡：支持快速报告/项目研究切换、可搜索多 Collection、显式 Project、已有/新建 Task、报告与 Agent 高级参数、持久命令轮询和原位失败恢复。旧 AgentPanel 继续作为高级手工路径。
- 定向后端测试覆盖同 key 重放/冲突、12 路并发双击仅一条 Command/job、快速报告唯一目标、项目新建与已有 Task、部分资源保留恢复，以及目标完成后 Command 终态同步。前端测试覆盖双模式、Collection 范围、显式 Project 门禁、本地恢复和幂等请求头。
- 阶段收口时后端全量回归为 170 passed、3 skipped；前端为 35 passed，production build 通过。Ruff check、Compose 配置与 Git diff 检查通过。

### 已知边界与下一步

- Command 负责可恢复编排，不复制报告或 AgentRun 内容；目标资源及 durable job 仍是执行事实，Runtime 只做统一投影。
- 当前报告检索仍需在阶段五完成“向量只返回候选 ID、SQLite 正式 Core 重水化”切换；阶段六再补 stale_context 和 needs_review 的完整 PC 生命周期。

### 可复用的项目成果表述

- 将快速报告与治理型项目研究收敛为一个持久、幂等、可恢复的双模式研究指令入口，以稳定资源 ID 和阶段状态机消除双击、断网重试及中断恢复造成的重复 Task、Snapshot、Run 或报告。

## 2026-08-21：阶段三——统一运行可观测性

### 问题与架构决策

- 旧 Runtime 由 React 分别拉取最多 100 条 ingestion 和 report 后在浏览器拼接，看不到 AgentRun、Collection 同步、projection outbox、Worker 心跳、队列位置或 lease owner，因此无法回答“为什么 queued”“图关系为什么为 0”“报告停在哪一步”。
- 新增只读 `RuntimeObservabilityService`，直接从权威业务表构建有界投影，不创建第二份任务事实。`/api/v1/runtime/overview` 返回 API、Worker、LLM、Qdrant、Neo4j、Vault、执行器心跳、分类计数和 projection backlog；`/api/v1/runtime/work` 使用 keyset cursor，并支持 kind/status 筛选。
- `RuntimeWorkItem` 统一呈现业务状态、队列状态、当前阶段、attempt、优先级、队列位置、lease、执行器、最近错误、详情深链和恢复能力。失败投影使用专属定点重试接口；报告、入库和 AgentRun 仍走各自恢复接口。

### 修改范围与验证

- 新增 `app/runtime` 服务与 schema、失败投影原子重排、三个 Runtime v1 API；React 运行台加入服务状态、Worker 最后心跳/当前任务、LLM 配置提示、统一任务筛选与分页、投影 backlog、长错误内部滚动和安全重试。
- 后端全量回归为 164 passed、3 skipped；前端为 32 passed，production build 通过。Ruff、Compose 配置和 Git diff 检查通过。
- 定向测试覆盖 overview、三类 durable job 聚合、cursor 翻页、kind/status 筛选、非法 cursor、投影失败与二次重试拒绝；组件测试覆盖 Worker 离线且 AgentRun queued 的可行动提示，以及失败投影原位恢复。

### 已知边界与下一步

- 当前服务探针为本地只读 TCP/目录检查，不代表 Qdrant/Neo4j 数据内容完全一致；projection backlog 和事件错误用于定位一致性缺口，正式重建命令在阶段五实现。
- 当前 Runtime 操作的是底层资源任务。阶段四将增加持久 ResearchCommand，使快速报告和项目研究共享更符合用户语言的编排进度。

### 可复用的项目成果表述

- 建立覆盖五类长任务的统一 Runtime 可观测投影，以游标分页呈现业务/队列双状态、租约 owner 和投影 backlog，并在 React 中把 Worker 离线、AgentRun 排队和 Neo4j 投影失败转化为可直接执行的诊断与恢复动作。

## 2026-08-21：阶段二——统一任务执行平面

### 问题与架构决策

- 旧实现同时存在 FastAPI 内置入库/报告 Dispatcher 与独立 Worker，长任务可能被两个执行器竞争领取；Collection 同步和 AgentRun 又只由 Worker 处理，形成两套恢复语义。
- FastAPI 现只持久化业务资源与队列意图，不在进程内执行长任务。独立 Worker 统一消费 ingestion、report、agent_run、collection_sync 和 projection outbox，默认并发度为 1。
- schema v16 以向前兼容迁移增加任务优先级、`lease_owner`、投影所有者与执行器心跳。交互式提交/显式启动使用高优先级，同优先级保持创建顺序。
- 每类任务按 lease/3 续租；`job_id + attempt + lease_owner` 成为执行 fencing token。入库候选/终态、报告阶段/终态、AgentRun 事件/工具/输出/Platform Finalizer，以及投影完成/失败都在业务写事务内验证所有权。
- Worker 周期性恢复过期租约；API 重启不再改变队列或执行状态。投影 outbox 的终态与 ingestion 聚合状态在同一事务内刷新。

### 修改范围与验证

- 修改 queue repository、ingestion/report service、Agent Runtime/Repository、Platform Finalizer、统一 Worker、API lifespan、健康投影与对应测试；未读取、迁移或改写保留的 operational 数据和 `data/real_world_test/`。
- 后端全量回归为 161 passed、3 skipped；新增短租约慢投影、双 owner、交互优先级和 AgentRun stale owner 回归。Ruff check、前端 28 项 Vitest、production build、Compose 配置和 Git diff 检查通过。
- 短租约测试确认投影执行期间第二 Worker 无法重复领取；旧 AgentRun/投影 owner 无法在新 attempt 后写阶段或提交终态。API 测试确认创建后保持 queued，直到独立 Worker 消费。

### 已知边界与下一步

- 当前健康接口仅给出统一 Worker 的粗粒度在线状态；队列位置、当前任务、AgentRun、投影 backlog 和失败恢复将在阶段三的 Runtime v1 接口中补齐。
- schema v16 是当前代码契约；任何用户数据库只有在正常启动 Repository 时才执行迁移。本阶段测试仅在临时数据库验证迁移完整性和 `integrity_check`，未直接迁移用户数据。

### 可复用的项目成果表述

- 将 API 内置调度与通用 Worker 的双执行面收敛为单一持久 Worker，通过优先级队列、执行器心跳、lease/3 续租和三元 fencing token，统一保障 PDF 入库、报告、AgentRun、集合同步与外部投影的崩溃恢复和旧执行隔离。

## 2026-08-21：阶段一——统一 PC 部署与入口

### 决策与实现

- React 确认为唯一日常产品入口；Streamlit 固定为 8501 端口的运营控制台。
- 新增 Node 22 构建、Nginx 静态托管、同源 `/api` 反向代理和 SPA 深链回退；Compose 现在声明 React、API、worker、Qdrant、Neo4j 和 Streamlit 的完整本地栈。
- React 的运营控制台地址改为 `VITE_OPERATIONS_URL` 配置，Vite 开发代理统一使用 API 8000，不再保留 8010/8503 的隐式本地约定。
- 新增 `make up`/`make dev` 统一启动入口，并保留 `make test` 作为后端、Ruff、前端测试和生产构建的组合检查。

### 验证与边界

- 前端 28 项 Vitest、TypeScript 和 Vite production build 通过，`docker compose config --quiet` 通过，Git diff 格式检查通过。
- Web 镜像构建定义已进入 Compose；当前机器配置的 Docker registry mirror 对 Node/Nginx 基础镜像返回 401，因此镜像下载型构建需在镜像源恢复后再次执行。这是本机镜像源问题，不是前端编译或 Compose 结构错误。
- 本阶段未修改任何 operational SQLite、Qdrant、Neo4j、Vault 或 `data/real_world_test/` 内容。

### 可复用的项目成果表述

- 将分离运行的 React 开发服务器收敛为 Nginx 托管的同源 PC 工作台，补齐 API 代理、SPA 深链回退和一键 Compose 启动，同时把 Streamlit 明确降级为独立运维入口。

## 2026-08-19：15 篇真实文献验证与 React 主工作台

### 目标与边界

将日常研究操作收敛到 React 主工作台，保留 Streamlit 作为运营诊断控制台；同时把每篇 PDF 的处理上限提升到 150 页，并用已有的 15 篇完整真实文献检查长文档路径。首次实现阶段没有自动新建第二轮真实 LLM 入库；随后在用户逐次确认清理旧运行数据和发送抽取文本后，已在全新隔离环境完成 150 页真实重测。

### 现场数据与验证

- 15 篇 PDF 原始共 687 页；150 页解析策略覆盖全部 687 页，最长单篇 118 页、累计抽取 2,361,080 个字符。旧 100 页策略处理 669 页，因此本次解除 18 页截断。
- 既有真实 LLM 基线（100 页策略）产生 308 个候选：批量发布 120 个正式实体、剩余 188 个候选待审核；Qdrant 689 个切片，报告成功样本的四项质量指标均为 1.00，失败样本引用覆盖为 0.875，均保留在隔离真实环境目录。
- 150 页真实重测在清空旧 SQLite、专属 Qdrant 集合和 Neo4j topic 后运行：15/15 文档、687 页、707 个切片、286 个候选。批量发布 109 个无冲突实体，保留 3 个实体冲突和 174 条端点受阻关系；109 条投影 outbox 均完成，Qdrant 为 707 个点，Neo4j 新 topic 为 109 个实体、0 条正式关系。
- 新报告基于新集合中的 8 条正式证据一次生成完成；证据落地、引用覆盖、引用忠实和结构完整度均为 1.00，8/8 证据被引用，无无效引用。运行记录为 7,085 输入 token、4,488 输出 token、100,907.32 ms；Provider 单价仍未配置，因此 `cost_usd` 为 `null`。
- React 审核台改为服务端 25 条分页。真实队列初始为 `1–25 / 188`；`≥90%` 置信度筛选为 185 条；按一篇文献 ID 筛选为 12 条。关系候选显示源/目标实体名称而非内部 UUID。
- 浏览器验收覆盖知识库、审核台、报告、运行记录和 Streamlit 运营概览：React 显示 150 页策略与真实模型已配置；报告同时呈现完成与质量门失败状态、证据包和质量指标；Streamlit 显示队列、服务健康和恢复边界，无 `DeltaGenerator` 渲染泄漏。

### 实现与可靠性

- 新增 React 的知识库、研究报告、运行记录页面；侧栏将 Streamlit 定位为“运营控制台”，日常入库、检索、审核和阅读报告使用 React。
- 候选页接口支持类型、最低置信度和文献 ID 筛选，返回总数、游标式下一页和关系显示名，避免大队列一次性进入浏览器。
- 抽取的固定七段上下文改为“开头—均匀中段—结尾”采样，覆盖方法、实验和限制部分，但仍保持模型提示词上限。
- 结构迁移在 `BEGIN IMMEDIATE` 之后重新检查版本，消除 API 与 worker 首次并发启动时的重复列竞争；新增并发回归测试。
- React 健康检查迁入 `/api/knowledge/health`，解决开发代理下状态灯误报“未配置真实 LLM”的问题；原 `/health` 保持给 Streamlit 和服务探针使用。

### 可复用的项目成果表述

- 将 PDF 研究系统从单一 Streamlit 界面演进为 React 主工作台与 Streamlit 运营控制台的双层架构，并通过服务端分页将 188 条真实审核候选的首屏渲染稳定控制在 25 条。
- 将单篇文献处理上限从 100 页提升到 150 页，在 15 篇真实 PDF、687 页的环境中验证全文解析覆盖；同时以分层采样保持 LLM 抽取提示词规模受控。
- 在清理旧运行状态后，以同一 15 篇真实 PDF 从零重跑 150 页真实 LLM 入库、审核发布、向量/图投影、正式检索和带引用报告，保留候选冲突与关系依赖，不以测试便利绕过人工审核。
- 针对多进程首次启动的 SQLite 结构迁移竞争，使用写锁后重检版本的事务策略并补充并发测试，避免重复 `ALTER TABLE` 导致服务不可用。

## 2026-08-20：PC 工作台闭环与长任务可靠性复核

### 范围决策

- 当前产品只验收 PC 工作台，目标视口为 1440×900 与 1920×1080；移动端导航和响应式适配暂不投入。
- 150 页是现阶段可调整的单篇解析配置，不作为长期产品契约。已有 15 篇真实 PDF 的范围为 13–118 页，因此本轮验证其 687 页均能完整解析，不再增加 149/150/151 页的严格边界夹具。
- 外部数据可以进入隔离测试环境，但不得改写主 SQLite、Qdrant、Neo4j 或用户保留的真实测试快照，也不为测试绕过人工审核边界。

### 实现与问题修复

- React 知识库改为“提交并开始入库”，由内置入库执行器领取明确的 ingestion；失败任务可在原卡片重试，活动任务自适应轮询。运行页同时显示报告和入库执行器状态，不再要求普通用户打开终端启动通用 Worker。
- 入库与报告共用租约心跳和 attempt fencing。入库候选按单篇文献在事务内替换，过期旧执行者不能继续写候选或覆盖新 attempt 的终态，降低长 PDF/真实 LLM 超时后重复调用与重复候选风险。
- 真实 LLM 门禁覆盖提交、已有任务启动、失败重试和重启恢复。mock 配置下，手动动作在改动队列前返回 409；已领取的恢复任务在 PDF 解析、Core/Qdrant 写入和候选抽取前将 ingestion/job 原子置为 failed，避免部分副作用。
- Neo4j 关系投影现在必须返回实际关系 ID；当任一端点缺失时，outbox 明确失败并保留可重试错误，不再把 Cypher 的静默 no-op 误记为 completed。该缺陷是“SQLite 有关系、图中关系为 0”的直接风险来源之一。
- Core 证据引用匹配改为大小写不敏感，修复原文大小写差异导致 Paper 候选不能发布的问题；高置信度自动审核按 100 条分片，避免超过单次批量 API 上限；v9 集合授权以正式 Paper 与当前 Collection membership 的交集为准，移动文献后旧项目范围立即失效。
- PC 布局将空的 Memory 提案区收为紧凑提示，候选审核获得完整宽度；知识入库、报告和运行历史使用有界内部滚动；AgentRun 的 grid、Card 和 trace 修复 `min-width` 链路，1440 宽度不再发生整页横向溢出；系统 inbox 不再出现在项目知识范围选择器中。

### 真实数据复核与产品含义

- 原始 15 篇 PDF 均可读取、未加密、每页有文本，文件内容哈希与真实测试数据库 15/15 一致。真实快照为 15 documents、707 chunks、110 published entities、164 published relations；知识库只显示 14 篇 Paper 是因为一篇 Paper 及其相关关系仍处于 draft，并非文档缺失。
- 使用正式 Paper 白名单检索时，API 可返回 8 条证据并扩展出 20 条图关系，说明图关系并非全局为 0。现有完成报告的机械质量门均为 1.00，但 8 条证据仅来自 2 篇论文，且部分证据是参考文献或与问题弱相关；后续质量门应增加来源多样性和检索相关性，而不能只看引用格式完整。
- 三篇论文标题仍被 PDF 页眉误识别，全部 source URL 为空；一篇全文包含 NUL 控制字符，Vault 另有少量文件名尾空格造成的失效 wikilink。这些问题不会阻断本轮 PC 主流程，但应进入下一阶段的数据清洗与来源溯源任务。

### 验证结果

- 全量后端回归为 156 passed、3 skipped；Ruff 全量检查通过。前端 Vitest 为 28 passed，TypeScript 与 Vite production build 通过。
- `pip check` 与 `docker compose config --quiet` 通过；本地 Qdrant 1.18.0 和 Neo4j 5 Community 均为 healthy。浏览器和隔离 API/Vite 服务在验收后停止。
- 15 篇原始 PDF 在临时 SQLite/Vault、确定性抽取器和 Noop 外部投影中重新执行：15 documents、687 pages、707 chunks、45 drafts，11.599 秒完成；1 秒租约下成功续期 30 次，attempts 保持 1，运行中第二次领取被拒绝，`integrity_check=ok`。临时目录退出后删除。
- 真实数据副本的浏览器验收覆盖 1440×900 与 1920×1080：知识库、审核、报告、运行页均无横向溢出或全屏错误；GraphRAG 查询返回 8 条正式证据和关系扩展；queued 报告可在页面启动，失败后出现原位清理重试入口。验收副本和本地服务已清理。

### 后续优先级

1. 首页指令栏进一步收敛 Project、Task、ContextSnapshot 与 AgentRun 的普通流程，实现“一条研究指令→受控范围→可见进度→报告/Artifact”的单入口；高级面板保留显式治理能力。
2. 为入库增加拖放、多文件/URL 预检、去重和逐篇进度；为运行记录增加类型/状态筛选、AgentRun 汇总与服务端分页。
3. 修正 PDF 标题、来源 URL、控制字符和 Vault 链接；建立检索相关性与来源多样性的人工抽样基线。
4. Dashboard 使用服务端 totals，避免当前有界最近列表在数据超过 12 条时被误读为总量；Collection 选择改为可搜索多选并记忆上次范围。

### 可复用的项目成果表述

- 将 PDF 入库和证据报告改造成 React 中可直接发起、可观测、可恢复的持久任务闭环，并通过租约心跳、attempt fencing 和事务终态阻止超时执行者重复写入或覆盖新任务。
- 定位并修复 Neo4j `MATCH` 缺端点时的静默关系丢失，使投影 outbox 从“假完成”变为可诊断、可重试失败；同时用正式图检索验证关系扩展可返回结果。
- 基于 15 篇、687 页真实论文数据完成隔离数据质量与 PC 工作流复核，并把引用格式评分与检索相关性/来源多样性区分开，避免用机械 1.00 指标夸大报告质量。

## 2026-08-04：v1 归档与 Knowledge Core 主线收敛

### 决策

不再兼容 v1。以纯 v1 提交 `1fb7dc6` 创建 `v1-archive-1fb7dc6` tag 和 `archive/v1/` 可运行快照；历史源码、测试、配置、依赖和用户数据不删除。根项目只保留 Research Knowledge Core。

### 实现

- 活动树移出 Planner、Search Agent、13 节点 ResearchState、旧 GraphRAG、Memory、Evaluation、旧报告 API/UI/CLI 和旧 Obsidian Exporter。
- API 只创建 SQLite 持久任务；同镜像单 worker 使用租约执行入库、报告和 projection outbox。
- 审核采用 `BEGIN IMMEDIATE` 与 `UPDATE ... WHERE status='draft'`，正式事实、review event、outbox 同事务提交。
- ingestion 增加 `publishing`；Neo4j/Obsidian 故障可重放，下游不是业务事实源。
- 报告只检索已发布 paper 白名单中的 Qdrant 切片和正式 Neo4j 路径，最多修订一次。
- 新增 30 问题/10 PDF 评测集和 Recall@5 runner。

### 验证

- `archive/v1` 原 38 项测试独立通过。
- 根 knowledge-only 测试覆盖并发幂等、347 候选统计、租约恢复、投影故障、人工笔记保留、报告质量与 API。
- 真实 Qdrant 1.18 与 Neo4j 5 幂等集成测试各 1 项通过。
- 固定 10 PDF / 30 问题 hash 检索基准达到 Recall@5 1.00、grounding 1.00、平均 63.92 ms。
- 隔离浏览器 E2E 贯通入库、3 候选审核、正式投影、报告生成与 p.1 原文证据包，四项报告质量指标均为 100%。
- 真实 LLM 调用改为只发送代码内合成证据，避免外传本地知识库；实际模型结果记录于 2026-08-05 条目。

### 简历口径

可以陈述完成了 v1 冻结归档、主线边界收敛、SQLite 事务审核与 outbox、一致性恢复和正式证据报告；效果指标只引用实际执行并保存结果的基准。

## 2026-08-05：真实 LLM 与 Docker 发布链路收尾

### 数据与权限边界

真实 LLM smoke 不读取 `data/` 或现有知识库，只在 pytest 临时目录创建 SQLite，并向 Provider 发送两段代码内合成证据。测试最多修订一次，避免不可控调用。Docker 验收只创建本地镜像和临时容器，不修改 Docker Desktop 的全局 mirror 配置。

### 验证

- `RUN_LIVE_LLM_INTEGRATION=1 .venv/bin/pytest -q tests/test_report_live_integration.py`：1 项通过。
- DeepSeek `deepseek-v4-flash`：1 次调用，274 输入 token、217 输出 token、4,642.56 ms。
- grounding、引用覆盖、引用忠实度和结构评分均为 1.00，未触发修订。
- Provider 单价未配置，`cost_usd` 如实为 `null`。
- `docker build --pull=false -t research-knowledge-core:verification .`：成功。
- 容器内确认没有 `/app/archive`、没有 `/api/research`，并真实启动后得到 `/health` 200。

### 结论

真实模型适配、质量门、运行元数据和 Docker 发布边界均已形成可复现证据。合成 smoke 不能替代真实论文报告的人工质量评估；美元成本必须在配置明确单价后再记录。

## 2026-08-04：Knowledge Base v2 批量审核行为与幂等性核查

### 用户场景

用户在 Streamlit 审核队列点击“批准所有无冲突候选”后，任务仍为 `needs_review`，页面显示：

- 候选总数：347
- 待审核：20
- 实体：7
- 关系：13

需要解释剩余内容的含义，并判断多次重复点击是否会污染 SQLite 审计数据、Neo4j 正式图谱或 Obsidian 投影。

### 统计口径与调用链

页面先读取任务的全部候选；“候选总数”包含 `draft`、`published`、`merged` 和 `rejected` 等所有状态。其余三个数字只基于 `status == "draft"` 的候选统计，因此本次 7 个实体与 13 条关系共同构成 20 个待审核项。

批量按钮调用：

```text
Streamlit 审核队列
  -> POST /api/knowledge/ingestions/{ingestion_id}/approve-ready
  -> KnowledgeIngestionService.approve_ready
     1. 批准没有 merge_suggestions 的实体
     2. 批准两个端点均已有 canonical_id 的关系
     3. 仍有 draft 时保持 needs_review，否则转为 completed
```

### 现场数据验证

任务：`ing-052c0d73ec49497b8c8ef9af05f0ced5`（Agent 智能体研究）。对 `data/knowledge/knowledge.db` 进行只读查询，结果为：

| 状态 | 类型 | 数量 |
| --- | --- | ---: |
| `published` | entity | 129 |
| `published` | relation | 198 |
| `draft` | entity | 7 |
| `draft` | relation | 13 |

总计 347；已发布 327；待审核 20。`review_events` 共有 327 条，且 327 个已发布候选各对应一次审核事件，没有发现重复审核事件。

7 个待审核实体均有 1 个 `exact` 合并建议：思维链、ReAct、HotPotQA、Chain-of-Thought (CoT) prompting、Language Agent、Large Language Model (LLM)、Large Language Model。13 条待审核关系全部至少有一个端点指向这 7 个尚未取得 `canonical_id` 的实体，因此关系会继续留在队列中。

### 结论

在当前页面状态下，按顺序多次点击批量按钮不会重复创建实体、关系或审核事件：

- 7 个实体因为已有合并建议，会被批处理主动跳过，等待人工选择“确认合并”或在允许时明确批准为新实体。
- 13 条关系因为端点尚未发布，发布校验失败后保持 `draft`，不会写入正式关系表和审核事件表。
- 不会重新调用 LLM、重新解析 PDF、重新写入 Qdrant，也不会重跑知识抽取。
- 每次请求仍会产生少量 SQLite 查询、关系依赖校验和任务状态刷新；服务重启后的首次正式图谱预检还可能访问 Neo4j。数据风险很低，但重复点击没有推进作用，且当前成功提示容易让用户误以为所有内容都已处理。

建议操作顺序：先逐一审查并合并 7 个冲突实体，再点击一次“批准所有无冲突候选”，系统即可继续发布其余端点已满足的关系。若最终没有 `draft`，任务状态会变为 `completed`。

### 幂等性边界与后续改进

当前结论适用于 Streamlit 页面上的顺序重复点击。接口尚未实现严格的并发幂等保护：两个并发 `approve-ready` 请求可能同时读取同一批 `draft`；单候选 decision 接口也没有先拒绝已终态候选。页面的同步请求与立即 rerun 使该风险较低，但直接 API 重放或并发调用仍是需要补强的工程边界。

后续建议：

1. decision 写入使用条件更新或事务锁，只允许 `draft` 状态进入终态。
2. 为已发布关系建立候选来源唯一约束或幂等键。
3. 批量接口返回 `published`、`skipped_conflict`、`blocked_relation` 的数量，页面据此显示准确反馈。
4. 当只剩冲突实体和被阻塞关系时，隐藏或禁用批量按钮，并提示下一步先处理实体合并。
5. 增加顺序重放与并发重放测试，覆盖 SQLite、Neo4j 投影和审核事件三层一致性。

### 可复用的项目成果表述

- 设计并实现 PDF 到结构化知识的人工审核发布链路，以 SQLite 保存候选与审计事实，以 Neo4j、Qdrant 和 Obsidian 分别承担图投影、向量检索和可读知识库职责。
- 针对 347 个真实候选完成批量审核行为核查，通过候选状态、关系端点和审核事件三方对账定位 7 个实体冲突及 13 条依赖阻塞关系，并识别并发幂等边界。
- 将批处理结果从单一成功语义拆解为发布、冲突跳过和依赖阻塞，为后续可观测性、交互反馈与并发安全改造形成明确方案。
