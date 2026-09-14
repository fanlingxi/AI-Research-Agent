# 安装、验证与演示

以下 PowerShell 命令从仓库根目录执行。准备 Python 3.11+、Node.js 22 LTS 和 `frontend/package.json` 声明的 pnpm；依赖版本以项目文件为准。

## 安装与离线验证

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Push-Location frontend
pnpm install --frozen-lockfile
pnpm test
pnpm build
Pop-Location
$env:PYTHONUTF8 = '1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider
.\.venv\Scripts\python.exe -m ruff check --no-cache .
```

Windows 使用 `requirements.txt`；`requirements.lock` 是历史 macOS 环境快照。离线测试使用临时数据库和模型替身，不需真实密钥或服务，也不会向生产知识写入测试资料。外部集成测试需要显式开启。

## 本地工作台

```powershell
powershell -ExecutionPolicy Bypass -File scripts/local.ps1 start
powershell -ExecutionPolicy Bypass -File scripts/local.ps1 status
# 使用完毕
powershell -ExecutionPolicy Bypass -File scripts/local.ps1 stop
```

首次启动从 `.env.example` 创建被忽略的 `.env`，保留已有配置。工作台在 `http://127.0.0.1:5173`，API 文档在 `http://127.0.0.1:8000/docs`，运维台在 `http://127.0.0.1:8501`。脚本在后台启动 API、Worker、前端及运维台，记录位于 `data/runtime/local/`。

无模型和检索服务时可检查项目管理、运行状态及确定性演示；真实抽取和报告需要配置模型提供方、模型名及对应密钥。完整知识投影还需要 Qdrant、Neo4j；可使用现有 `docker compose up -d qdrant neo4j`，先设置本地 Neo4j 密码。缺依赖时页面显示降级。`.env` 不进入版本控制。

## 可选本地向量服务

Windows 可使用固定官方发布包，不依赖 Docker：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/vector-service.ps1 install
powershell -ExecutionPolicy Bypass -File scripts/vector-service.ps1 start
powershell -ExecutionPolicy Bypass -File scripts/vector-service.ps1 status
```

安装器验证 SHA256；服务监听 `127.0.0.1:6333`，存储位于 `data/runtime/qdrant/storage`。同一端口只启动一种 Qdrant 实例。`stop` / `restart` 仅管理脚本记录的进程。runtime 内含持久数据，不能作为普通日志清理。

新安装默认 `hash` 向量，用于确定性开发，不能代表语义检索质量。使用固定 Qwen 语义模型前显式安装依赖与下载权重：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-embeddings.txt
.\.venv\Scripts\hf.exe download Qwen/Qwen3-Embedding-0.6B --revision 97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3 --cache-dir data/models/embeddings
```

`.env` 配置：

```dotenv
EMBEDDING_PROVIDER=qwen3-local
EMBEDDING_DIMENSION=1024
EMBEDDING_DEVICE=cpu
EMBEDDING_CACHE_DIR=data/models/embeddings
EMBEDDING_BATCH_SIZE=2
QDRANT_URL=http://127.0.0.1:6333
```

GPU 环境先安装与硬件匹配的 PyTorch，再改为 `cuda`。缺权重、设备不可用和超长输入均明确失败。模型身份进入 collection 后缀，旧索引保留；配置后重启 API/Worker。为现有已审核资料执行 `python -m app.knowledge.rebuild --target qdrant` 会重建当前配置指定的派生 collection；先核对目标。project_run 的向量候选与 `dense-v1` 仍需显式选择，服务就绪不会自动切换检索默认。

## SQLite 备份与恢复

停止所有写入知识库和 checkpoint 的进程后，在同一停机窗口执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/local.ps1 stop
.\.venv\Scripts\python.exe -m app.persistence.backup backup data/knowledge/knowledge.db data/backups/release/knowledge
.\.venv\Scripts\python.exe -m app.persistence.backup backup data/runtime/agent_checkpoints.db data/backups/release/checkpoints
.\.venv\Scripts\python.exe -m app.persistence.backup restore data/backups/release/knowledge data/backups/release/restored-knowledge
.\.venv\Scripts\python.exe -m app.persistence.backup restore data/backups/release/checkpoints data/backups/release/restored-checkpoints
```

目标目录必须尚不存在。SQLite backup API 包含已提交 WAL，验证完整性及校验和；每个数据库独立一致，在线两库备份并不原子。恢复文件在新目录 `database.db`，核验后再配置数据路径。原始 PDF 需另行备份；向量、图和 Vault 是可重建投影。

## 演示与验收

```powershell
.\.venv\Scripts\python.exe -m app.benchmarking --manifest benchmarks/phase6/cases/v1/manifest.json --output data/reports/phase6
.\.venv\Scripts\python.exe scripts/run_phase6_demo.py --output data/reports/phase6
```

先观察确定性演示的范围隔离、引用、运行状态及失败处理，再在真实服务中导入有权使用的资料、审核、绑定任务范围、构建快照并生成研究报告。核验每条结论与原文、证据不足时的缺口说明，以及反馈后的新运行和旧产物可追溯性。

上述演示不是最新真实模型对照的复刻。付费评测必须单独确定模型、隔离数据域、调用限制及预算；测试样本中的合成授权无此效力。历史实验档案和个人审核记录不随公开仓库分发。已知结果及待验收边界见[项目状态](PROJECT_STATUS.md)。
