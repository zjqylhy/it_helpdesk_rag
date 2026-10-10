# 知答 · IT 知识助手

用于求职展示的知识问答项目。首期提供 Web 问答、知识管理和离线评测，需求与技术方案已由用户确认。

Windows 可使用 Docker Desktop 部署完整应用，也可按 [Windows 原生运行说明](docs/windows.md) 开发调试。

云上支持 Linux 单机 Docker Compose，共用应用代码，见 [云部署说明](docs/cloud.md)。

业务 API 使用 Go/Gin，RAG 与 Worker 使用 Python/FastAPI，前端使用 Vue 3，数据保存到 PostgreSQL/pgvector。中文 BM25 与向量检索经 RRF 融合后调用 Qwen 重排，DeepSeek 根据证据生成带来源的回答。

## 已实现

- 管理员/普通用户登录、会话归属校验、历史记录、请求幂等、单会话生成互斥。
- PDF 文本、DOCX 段落、Markdown、UTF-8 TXT 上传，最大 20 MiB；版本、任务进度、切片预览、重复内容确认和失败重试。
- 异步任务租约、恢复、Embedding 校验、事务发布和不可变知识快照；失败保留旧版，停用后新请求排除对应文档。
- 多轮指代改写、双路检索、重排、证据充分性判定、拒答/澄清、POST SSE、引用快照、受控原文件下载。
- JSONL 评测集修改、人工审核、冻结发布、文档变化影响清单、AI 修订建议和新题候选。
- 四组检索、端到端重放、逐事实辅助评分、Ragas 忠实度、人工裁决、报告及清单一致续跑。
- 默认业务知识库联合问答、可选单/多库筛选、适用学校澄清与追问承接；引用显示所属知识库和组织。

工程验证与真实模型协议联调已通过：DeepSeek JSON/流式输出、Qwen Embedding（1024 维）与重排序可用。已导入 9 份真实素材，分为 7 个知识库；两题修订开发集已由用户裁决为全部通过。30 条开发题已审核、冻结并完成真实模型评测：四组 Recall@5 为 0.82/0.88/0.92/1.00，最终方案可回答题经人工裁决为 24/25，3 条拒答、2 条澄清通过，执行失败为 0。

200 题测试集已由用户审核并冻结为 `test-200-v1`，完成四组检索与 800 条真实问答。共享改写的检索 Recall@5 为 78.65%/81.04%/81.35%/94.48%，端到端辅助答案正确率为 66.25%/73.75%/72.50%/90.00%，均无执行错误。用户已确认 D 全部 200 条实际答案裁决：正式答案正确率 144/160（90%），拒答 25/25、澄清 13/15，待人工裁决 0；多文档题正式正确率仅 22/30（73.33%），仍是薄弱项。正式报告另存 `reports/test-200-v1/human-reviewed-D/`，结果页为其中的 `review.html`，原答案和辅助评分保留。本套只有 140 个事实主题，多文档题全部来自 Windows 库，不以总体均值宣称全部场景达标；浏览器延迟优化目标和云端镜像实际启动尚未验收。详见 [验证记录](docs/verification.md)。

以上质量成绩来自原 `qa-v2` 单库离线流程，不代表新增 `qa-v3-scope` 联合问答的效果；跨库增量已做工程回归和小样本真实验证，尚无独立跨库正式正确率。

## 问答范围与澄清

登录后默认“全部业务知识库”，直接输入问题即可。选择“指定知识库”后可勾选一个或多个库；切换范围开启新会话，历史会话会恢复原范围。管理员在知识管理中设置各库是否纳入默认问答，部署验证库已排除；指定该库时仍可查询。

- “VPN 怎么登录？”：没有学校上下文时询问学校并列出当前资料覆盖的学校；回复“交大”后承接原问题并给出引用。
- 已指定交大库：直接回答该校 VPN 问题，不重复询问学校。
- “交大和人大 VPN 登录账号分别是什么？”：分别引用两校资料作答；明确未收录组织时说明无对应资料。
- 跨库比较一次最多五库，为每个对象保留证据并检查引用覆盖；指定检索范围最多 32 库。通用问题仅返回相关 Top-5 证据，不把全库原文交给模型。

升级时 Go 启动会增量扩展范围字段并兼容旧会话，数据库和文件卷保留，已有资料无需重新向量化。Docker 按下述 Start 重建；原生源码运行需重启 Go 和 Python 服务以加载新代码。

## Docker Compose 启动（可选）

安装并启动 Docker Desktop，启用 Linux 容器。在项目根目录执行；已有 `.env` 时保留配置：

```powershell
# 首次初始化，已有 .env 则跳过，并填写模型地址和数据库密码。
.\tools\windows.ps1 -Action Init
.\tools\docker.ps1 -Action Check
.\tools\docker.ps1 -Action Start
```

浏览器打开 [http://localhost:3000](http://localhost:3000)。使用 `.env` 中的管理员账号登录，创建适用组织明确的知识库，再上传导出文件。任务完成后进入知识问答。脚本使用独立项目 `it-helpdesk-rag-docker`，数据库端口默认 55533，与原生环境分开；可用 `-WebPort` 和 `-PostgresPort` 调整端口，后续启动保持相同参数。

```powershell
.\tools\docker.ps1 -Action Status
.\tools\docker.ps1 -Action Logs
.\tools\docker.ps1 -Action Stop
```

Stop 删除本项目容器但保留命名卷，再次 Start 可恢复数据。Docker 数据库不会自动读取原生数据库的文档、会话或本机评测报告。云端使用相同 Compose 配置；初始化、HTTPS、升级与迁移见 [云部署说明](docs/cloud.md)。

配置要求：`INTERNAL_TOKEN` 使用至少 24 字符随机值；账号密码为 12–72 字节。`POSTGRES_PASSWORD` 使用随机字母数字，Compose 生成的连接 URL 会直接使用该值。模型 Key 仅配置在服务端 `.env` 或运行环境中，不传到浏览器。

模型凭据也支持运行环境注入，阿里兼容 `QWEN_API_KEY`；标准名称 `DASHSCOPE_API_KEY` 优先。Windows 用户/系统环境变量由本地启动脚本读取，Compose 将云端启动环境的 Key 显式传入 RAG 容器。

阿里 `DASHSCOPE_BASE_URL` 填带 `https://` 的业务空间主机根地址，不包含接口路径。Embedding 和 Rerank 路径由适配器追加；维度默认 1024。DeepSeek 默认非思考模式，模型名与已确认方案一致。

原文件和数据库保存在 Docker 命名卷中。Python 服务和 Go API 未发布到主机端口，浏览器经 Web 的 `/api/` 代理访问。数据库端口只绑定本机，供本地评测 CLI 使用。

2026-10-10 已在 Windows Docker Desktop 的 Linux 引擎实测：四服务健康、真实文件上传与问答引用、网页、容器重建持久化及独立数据库/文件备份恢复通过。首轮部署验证后，已通过上传 API 补齐原生环境的 9 份文档，保留原来的 7 个适用范围、649 个 1024 维切片；另保留部署验证库的 1 份打印机文档、72 个切片，合计 8 库、10 文档、721 切片。原生历史会话和冻结评测集未迁移；重新导入生成新 ID，不能直接复用原生冻结清单。容器内评测工具与 Ragas 适配器也已验证，实际云服务器与 HTTPS 仍待验收。详见 [验证记录](docs/verification.md)。

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
