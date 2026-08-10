# Migration Plan

## Research Knowledge Core → Personal Knowledge Agent Platform

## 1. 总体原则

采用渐进式迁移。

禁止：

-   推倒重写；
-   删除已有能力；
-   一次性大规模改造。

采用：

Migration + Adapter + Incremental Refactor。

## 2. 保留资产

必须保留：

-   SQLite事实源
-   PDF ingestion
-   Candidate
-   Review Event
-   Published Knowledge
-   Evidence
-   Qdrant
-   Neo4j Projection
-   Obsidian Export

## 3. 迁移阶段

Phase 0: 冻结当前版本。

Phase 1: Knowledge Core + Memory Core。

Phase 2: Context Builder。

Phase 3: Agent Runtime。

Phase 4: Workspace UI。

Phase 5: Domain Plugin。

## 4. Schema迁移

旧：

published_entities

published_relations

reports

新：

entities

relations

claims

evidences

projects

tasks

decisions

artifacts

## 5. 代码原则

业务逻辑禁止直接访问数据库。

采用：

Repository + Service + API

## 6. Agent原则

Agent：

读取知识。

提出建议。

生成产物。

禁止：

直接修改正式知识。
