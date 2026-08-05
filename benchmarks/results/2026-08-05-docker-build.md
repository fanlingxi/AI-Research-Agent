# Knowledge Core Docker 构建验收：2026-08-05

## 环境处理

Docker Desktop 配置的 Docker Hub mirror 对 `python:3.11-slim` 返回过 `401`。本次从 AWS ECR Public 的 Docker Official Images 副本拉取同名基础镜像，再在本机标记为 `python:3.11-slim`；未修改 Docker Desktop 全局配置。

- 基础镜像摘要：`sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93`
- 验收镜像：`research-knowledge-core:verification`
- 验收镜像 ID：`sha256:47648e695a1e5b4911a6d0b1afd6b907630ed29cc5764abf60eb62eddc7b118e`
- 镜像大小：226,377,622 bytes

## 验收结果

- `docker build --pull=false` 成功。
- Build context 为 975.22 kB，`.dockerignore` 生效。
- 容器内 `/app/archive` 不存在。
- 路由包含 `/api/knowledge/*`、`/api/reports/*`，不包含 `/api/research`。
- 镜像真实启动后 `GET /health` 返回 `HTTP/1.1 200 OK`，Schema version 为 4。
- 临时验收容器已停止并通过 `--rm` 自动删除。

健康检查中的 Qdrant/Neo4j 为不可用是预期结果：本次单容器探针没有把它加入 Compose 网络；真实存储连通性由独立容器集成测试覆盖。
