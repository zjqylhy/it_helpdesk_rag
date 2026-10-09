# 工程验证记录

日期：2026-10-09。环境：Windows、Go 1.27.1、Python 3.14.7、Node.js 24.21.0。另已安装本地 Python 3.12 环境供可选依赖使用。

| 验证 | 结果 | 范围 |
| --- | --- | --- |
| Go 编译、go test 与 go vet | 通过 | 业务 API 构建和静态检查；Go 包目前没有独立单元测试，其行为由跨服务集成脚本覆盖 |
| Vue/TypeScript 生产构建 | 通过 | vue-tsc 与 Vite |
| Python pytest | 15 项通过 | 向量顺序/数量、重排、流终态、预算、引用、跨切片证据、发布审核、防止 dev/test 主题泄漏、失败分母、未执行题保持待评估、语料版本比较、Ragas 模型适配 |
| Go + Python + SQL 集成 | 通过 | 管理员授权、会话归属、上传、重复检测、失败保旧、原子版本发布、旧引用、停用后双路排除、幂等、生成互斥、中断持久化 |
| 评测 CLI | 通过 | 真实 SQL 快照导出、来源核对、冻结发布、A/B/C/D 检索、生成/辅助评分、报告、一致清单续跑 |
| 无头 Edge 页面测试 | 通过 | 登录、知识库加载、流式答案、引用展开、知识管理、版本切片、登录恢复；无页面脚本异常 |

首轮 SQL 验证使用 PGlite 0.5.8 + pgvector + PostgreSQL TCP 协议服务，Go 使用 `DB_SIMPLE_PROTOCOL=true` 避免测试服务共享底层连接导致的语句缓存冲突。后续 Windows 原生验证见下文。当前环境没有 Docker，Compose 镜像构建与启动未验证。

## Windows 原生 PostgreSQL 补验

PostgreSQL 16.15 + pgvector 0.8.6 安装在项目 `data/native-postgres/`，仅监听 127.0.0.1:55433，不改动已有 5432 服务。独立测试库 `rag_contract_1791556708` 使用非超级用户 rag 和默认 pgx 协议（`DB_SIMPLE_PROTOCOL=false`）。

- `tests.integration_check` 通过：跨服务流程、原子版本发布、请求并发互斥和中断持久化。
- `tests.evaluation_check` 通过：来源核对、冻结发布、四组检索、生成评分、报告及续跑。
- 数据库正常 stop/start 后测试消息数量保留；重复 init 前后的 `.env` 配置一致，业务账号无超级用户权限，业务库 rag 无测试表或内容。
- Windows PowerShell 5 脚本语法与 Init 创建/拒绝覆盖验证通过；Check 能识别缺失配置并连接原生 pgvector；Python 启动工具语法检查通过。
- 官方独立 Compose v5.6.0 下载后通过 SHA256 校验，`config --quiet` 校验当前 Compose 配置通过，无需 Docker 引擎；CI 新增 Linux 镜像构建与启动健康检查任务，但本次未执行该任务。

以上模型调用均使用本地替身。原生环境的 Worker 崩溃后租约接管、应用进程崩溃恢复和真实 API 仍未专项验证。

模型替身服务来自 `rag/tests/fake_models.py`，测试文档明确为虚构的工程素材。替身回答与耗时不代表 DeepSeek/Qwen 的真实效果，不能使用这轮报告宣称达到业务指标。真实 Key、导出原文、开发集和 200 条经审核的测试题尚待提供/构建。

## 复现工程联测

先安装前端、RAG 和 tools 的依赖。在分别的终端启动以下服务：

1. `cd tools; npm.cmd ci; npm.cmd run test-db`：临时内存数据库监听 55432。
2. Go：设置 `DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/postgres?sslmode=disable`、`DB_SIMPLE_PROTOCOL=true`、`INTERNAL_TOKEN=local-test-internal-token-123456789`；账号配置为 admin / test-admin-password、demo / test-user-password，再从 backend 运行 `go run .`。
3. 模型替身：`cd rag; uv run uvicorn tests.fake_models:app --host 127.0.0.1 --port 8100`。
4. RAG：同一测试 DATABASE_URL / INTERNAL_TOKEN，设置两家 Base URL 为 `http://127.0.0.1:8100`、Key 为 `contract-test`、`EMBEDDING_DIMENSION=2`、`NO_PROXY=127.0.0.1,localhost`，运行 `uv run python -m helpdesk`。
5. `cd frontend; npm.cmd run dev`。

然后在 rag 终端使用相同测试配置执行 `uv run python -m tests.integration_check`、`uv run python -m tests.evaluation_check`；在 tools 执行 `npm.cmd run test-web`。测试结果和截图保存在忽略的 `data/` 目录。脚本使用固定的本机测试端口与测试账号，请在独立测试实例中运行。

本轮截图位于 `data/screenshots/`。真实服务联调完成后，应追加真实模型协议验证、真实语料规模、数据集清单、调用用量与质量报告，不覆盖本轮工程验证记录。
