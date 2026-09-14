# 开发与验证

安装、本地启动、向量服务与备份命令统一见[开始使用](GETTING_STARTED.md)。Windows 本地开发使用 `scripts/local.ps1`；Docker Compose 是另一种运行入口。`requirements-runtime.txt` 只包含运行依赖，`requirements-dev.txt` 在其基础上增加 pytest 和 Ruff；本地开发及 CI 安装后者，Docker 安装前者。`requirements.txt` 保留为开发安装兼容入口，供现有 CI 和旧命令使用，不重复声明依赖。可选本地编码器仍使用 `requirements-embeddings.txt`。这些声明不是完整依赖锁定文件。旧 macOS 快照 `requirements.lock` 已移除，可用 `git show ddb080e:requirements.lock` 查阅。

## 修改位置

接口保持薄层，业务流程放在对应领域服务，数据库操作通过 repository 完成。优先沿用当前 Worker、检查点、检索和提交机制。目录职责见[架构概览](architecture/overview.md)。

涉及范围、来源版本、快照、幂等或持久化时，要同时考虑旧运行和恢复过程。数据库结构使用增量迁移；外部模型调用在事务外执行，提交前重新验证状态。插件输出通过平台最终提交，待审记忆不能直接进入正式知识。

## 验证方式

从仓库根目录执行相关后端测试和 Ruff：

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_context_consistency.py
.\.venv\Scripts\python.exe -m ruff check --no-cache .
git diff --check
```

按改动选择相关测试文件；阶段交付或跨模块修改时去掉文件参数运行后端全量测试。前端行为修改在 `frontend` 下执行 `pnpm test` 和 `pnpm build`。纯文档修改检查内容、引用和差异即可。

测试使用临时数据库和模型替身。真实模型评测需要独立数据域、显式配置和调用限制；保留全部失败与未知用量，不能将完成率当作语义正确率。基准冻结文件的内容和指纹不可因文档整理而改写。

## 实验记录与评测工具

`app/experiments` 为工作台提供归档读取和标注汇总，API 不依赖 `app/benchmarking` 或 `scripts`。读取时保留目录边界、冻结指纹、来源范围和脱敏检查。共用文件指纹位于 `app/tools/file_hash.py`：文本统一换行后计算，PDF 按原始字节计算，保持现有冻结数据兼容。

离线与真实评测入口仍在 `app/benchmarking`，命令路径保持不变。`reading_trial`、`feedback_trial` 等模块仍被其他实验复用，不能直接作为一次性脚本删除；历史运行数据与版本标识也不随模块整理迁移。基准可用 `python -m scripts.validate_paper_benchmark` 校验，该命令不调用模型。

## 脚本入口

脚本各有用途，调用前使用 `--help` 查看参数；验收和代理连通性命令可能写入资料或发起真实请求。

| 入口 | 用途 |
| --- | --- |
| `scripts/local.ps1` | 本地 API、Worker 和 React 的启动与停止 |
| `scripts/vector-service.ps1` | 独立 Qdrant 服务的安装与管理 |
| `scripts/preview-run.ps1` | 将指定实验归档复制到隔离目录，只读预览报告 |
| `python -m scripts.run_phase6_demo` | 运行确定性离线演示，生成独立结果目录 |
| `python -m scripts.validate_paper_benchmark` / `scripts.validate_research_dataset` | 校验两种不同格式的冻结数据集 |
| `python -m scripts.manual_acceptance` | 对运行中的 API 执行人工验收流程 |
| `python -m scripts.test_openai_proxy` | 显式验证模型代理配置与连通性 |
| `python -m scripts.accept_phase6_baseline` | 将已审核、隔离且全部通过的结果登记为不可覆盖的基准 |

## 历史资料

旧版应用副本、阶段提示词、迁移计划和求职准备笔记已退出当前目录。清理前内容可在 Git 提交 `ddb080e56194d139f394ccb8ac3a049b8d909a14` 中查阅，例如：

```powershell
git show ddb080e:archive/v1/README.md
git show ddb080e:docs/migration/02_MIGRATION_PLAN.md
```

更早的 v1 另有 `v1-archive-1fb7dc6` 标签。保留历史用于追溯，不需要把整套旧应用放在当前运行树中。历史评测结果见[Phase 6 记录](evaluation/phase6-history.md)，当前状态统一见[项目状态](PROJECT_STATUS.md)。
