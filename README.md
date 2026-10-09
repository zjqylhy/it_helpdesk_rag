# 知答 · IT 知识助手

用于求职展示的知识问答项目。首期提供 Web 问答、知识管理和离线评测，需求与技术方案已由用户确认。

Windows 不需要 Docker Desktop，按 [Windows 原生运行说明](docs/windows.md) 配置数据库并启动。Docker Compose 是可选部署方案。

云上支持 Linux 单机 Docker Compose，共用应用代码，见 [云部署说明](docs/cloud.md)。

业务 API 使用 Go/Gin，RAG 与 Worker 使用 Python/FastAPI，前端使用 Vue 3，数据保存到 PostgreSQL/pgvector。中文 BM25 与向量检索经 RRF 融合后调用 Qwen 重排，DeepSeek 根据证据生成带来源的回答。

## 已实现

- 管理员/普通用户登录、会话归属校验、历史记录、请求幂等、单会话生成互斥。
- PDF 文本、DOCX 段落、Markdown、UTF-8 TXT 上传，最大 20 MiB；版本、任务进度、切片预览、重复内容确认和失败重试。
- 异步任务租约、恢复、Embedding 校验、事务发布和不可变知识快照；失败保留旧版，停用后新请求排除对应文档。
- 多轮指代改写、双路检索、重排、证据充分性判定、拒答/澄清、POST SSE、引用快照、受控原文件下载。
- JSONL 评测集修改、人工审核、冻结发布、文档变化影响清单、AI 修订建议和新题候选。
- 四组检索、端到端重放、逐事实辅助评分、Ragas 忠实度、人工裁决、报告及清单一致续跑。

工程验证与真实模型协议联调已通过：DeepSeek JSON/流式输出、Qwen Embedding（1024 维）与重排序可用。已导入 9 份真实素材，分为 7 个知识库；两题修订开发集已由用户裁决为全部通过。30 条开发题已审核、冻结并完成真实模型评测：四组 Recall@5 为 0.82/0.88/0.92/1.00，最终方案辅助事实评分为 24/25，3 条拒答、2 条澄清符合预期，执行失败为 0。实际新答案尚待人工裁决；200 条独立冻结测试样本及正式指标仍待构建和评测，当前结果不能证明业务指标达标。详见 [验证记录](docs/verification.md)。

## Docker Compose 启动（可选）

安装 Docker Desktop，并启用 Linux 容器。在项目根目录执行：

```powershell
Copy-Item .env.example .env
# 编辑 .env，填写模型 Key、业务空间 HTTPS 地址及本地账号。
docker compose up --build -d
```

浏览器打开 [http://localhost:3000](http://localhost:3000)。使用 `.env` 中的管理员账号登录，创建适用组织明确的知识库，再上传导出文件。任务完成后进入知识问答。

配置要求：`INTERNAL_TOKEN` 使用至少 24 字符随机值；账号密码为 12–72 字节。`POSTGRES_PASSWORD` 使用随机字母数字，Compose 生成的连接 URL 会直接使用该值。模型 Key 仅配置在服务端 `.env` 或运行环境中，不传到浏览器。

模型凭据也支持运行环境注入，阿里兼容 `QWEN_API_KEY`；标准名称 `DASHSCOPE_API_KEY` 优先。Windows 用户/系统环境变量由本地启动脚本读取，Compose 将云端启动环境的 Key 显式传入 RAG 容器。

阿里 `DASHSCOPE_BASE_URL` 填带 `https://` 的业务空间主机根地址，不包含接口路径。Embedding 和 Rerank 路径由适配器追加；维度默认 1024。DeepSeek 默认非思考模式，模型名与已确认方案一致。

原文件和数据库保存在 Docker 命名卷中。Python 服务和 Go API 未发布到主机端口，浏览器经 Web 的 `/api/` 代理访问。数据库端口只绑定本机，供本地评测 CLI 使用。

当前环境没有 Docker，Compose 尚未实际启动验证。Windows 原生 PostgreSQL 16.15 + pgvector 0.8.6 已验证跨服务流程、请求并发互斥和数据库重启持久化，详见 [验证记录](docs/verification.md)。

## Windows 本地开发（默认）

需要 Go 1.27、Python 3.12+、uv、Node.js 24，以及带 pgvector 的 PostgreSQL。推荐先按 [Windows 原生运行说明](docs/windows.md) 初始化数据库和配置，下面命令分别在独立终端执行：

```powershell
# 业务服务：自动创建首期数据库表及本地配置账号。
cd backend
go mod download
go run .
```

```powershell
# RAG + 导入 Worker；此入口处理 Windows 事件循环兼容。
cd rag
uv sync --frozen --extra test --extra faithfulness
uv run python -m helpdesk
```

```powershell
cd frontend
npm.cmd ci
npm.cmd run dev
```

开发页面为 [http://localhost:5173](http://localhost:5173)。Vite 将 `/api` 代理到 Go 的 8080 端口。Go 默认读取根目录 `.env`，Python 同样读取根目录配置；本地 Go 将上传文件写入绝对路径，Python 在同一主机读取。

## 离线评测

全部私有语料、样本和报告保存在忽略目录中。命令从 `rag` 目录执行：

```powershell
# 导出当前不可变快照，KB_ID 可在接口响应中获取。
uv run rag-eval export --kb KB_ID --out ../eval/private/corpus-v1.json

# AI 生成少量有来源的候选，随后人工编辑必要事实与证据。
uv run rag-eval generate --corpus ../eval/private/corpus-v1.json --count 5 --out ../eval/private/draft.jsonl
uv run rag-eval validate ../eval/private/draft.jsonl --corpus ../eval/private/corpus-v1.json

# 逐题人工确认，reviewer 填审核人，输出到新的草稿文件。
uv run rag-eval review ../eval/private/draft.jsonl --id CASE_ID --reviewer REVIEWER --out ../eval/private/reviewed.jsonl
uv run rag-eval publish ../eval/private/reviewed.jsonl --corpus ../eval/private/corpus-v1.json --directory ../eval/private/releases --version v1

# 同一问题改写结果用于四组检索对比；默认跑 dev，正式集加 --split test。
uv run rag-eval run ../eval/private/releases/v1/cases.jsonl --corpus ../eval/private/corpus-v1.json --variant all --out ../reports/retrieval-v1 --max-calls 200
uv run rag-eval compare ../reports/retrieval-v1/A ../reports/retrieval-v1/D --out ../reports/comparison.md

# 端到端评测与辅助评分。Ragas 会增加调用；每次请求/重试都计入预算。
uv run rag-eval run ../eval/private/releases/v1/cases.jsonl --corpus ../eval/private/corpus-v1.json --mode end-to-end --variant D --judge --ragas --out ../reports/answers-v1 --max-calls 200
```

`--max-calls` 是本次进程的模型 HTTP 请求上限，包含重试；不是金额承诺。达到预算后的题目明确记录错误，不当作正确或从分母中移除。续跑可追加 `--resume`，数据、代码 commit、模型与策略清单必须一致；每次续跑仍使用新的调用预算。金额需根据提供商实际计费和记录的用量核对，缺失用量记为 null。

报告包含 `manifest.json`、`cases.jsonl`、`summary.json`、`failures.md`。自动正确率是逐事实判定的辅助结果，正式正确率须通过 `adjudicate` 导入人工裁决；未完成必要复核时为 null。样本格式与维护操作见 [评测说明](docs/evaluation.md)。

## 验证

```powershell
cd backend
go test ./...
go vet ./...
```

```powershell
cd rag
uv run pytest -q
```

```powershell
cd frontend
npm.cmd run build
```

集成与浏览器验证命令、已通过项目和限制见 [验证记录](docs/verification.md)。Windows 真实模型探测使用 `tools/windows.ps1 -Action ModelCheck`；其他环境从 rag 执行 `uv run python -m helpdesk.probe`，配置完成后最多发出五次真实 HTTP 请求。

需求和设计分别见 [首期 spec](docs/spec-v1.md)、[技术方案](docs/technical-plan-v1.md)。飞书机器人、自动同步、OCR、PPT、企业 SSO、多实例和生产级压测属于后续阶段。
