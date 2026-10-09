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

SQL 验证使用 PGlite 0.5.8 + pgvector + PostgreSQL TCP 协议服务，不是原生 PostgreSQL。Go 使用 `DB_SIMPLE_PROTOCOL=true` 避免测试服务共享底层连接导致的语句缓存冲突。原生数据库保留默认 pgx 协议设置；发布事务、租约恢复和进程重启仍应在原生 PostgreSQL 中补验。当前环境没有 Docker，Compose 镜像构建与启动未验证。

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
