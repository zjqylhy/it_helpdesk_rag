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

模型替身服务来自 `rag/tests/fake_models.py`，测试文档明确为虚构的工程素材。替身回答与耗时不代表 DeepSeek/Qwen 的真实效果，不能使用这轮报告宣称达到业务指标。真实模型和语料的后续验证见下文；开发集和 200 条经审核的测试题尚待构建。

## 2026-10-10 真实模型协议联调

用户将 Key 配置在 Windows 用户环境变量 `DEEPSEEK_API_KEY` / `QWEN_API_KEY`。启动脚本补入当前进程未继承的凭据；Python 配置和云端 Compose 均支持 QWEN 别名，非空标准名 DASHSCOPE 优先。凭据未打印，也未复制到仓库配置。

通过 `tools/windows.ps1 -Action ModelCheck` 调用当前适配器，使用虚构短文本，共五次 HTTP 请求，无重试：

| 检查 | 结果 | 本次耗时 |
| --- | --- | --- |
| qwen3.7-text-embedding 文档输入 | 2 条，1024 维，数量/顺序/数值有效 | 0.44 秒 |
| qwen3.7-text-embedding 查询输入 | 1 条，1024 维，数量/数值有效 | 0.13 秒 |
| qwen3.7-text-rerank | 返回 2 条有效结果，相关文档排名首位 | 0.11 秒 |
| deepseek-v4-pro JSON 输出 | JSON 可解析，完整结束，符合短提示 | 0.53 秒 |
| deepseek-v4-pro 流式输出 | 正文非空，finish_reason=stop 且收到 DONE | 0.82 秒 |

记录用量：Embedding 58 + 20 token，Rerank 184 token，LLM 总计 73 token；金额未换算。以上耗时只代表本次小输入的协议检查，未验证真实业务文档上的端到端问答、RAG 指标或稳定性。

新增配置别名与优先级测试后 pytest 为 17 项通过。Windows Check 配置及数据库检查通过，Python 工具与 PowerShell 语法、Compose 静态配置均校验通过。

## 2026-10-10 本地应用启动验收

Go 首次使用根目录 `.env` 启动时暴露配置读取缺陷：`godotenv.Load(".env", "../.env")` 在缺少 backend/.env 时提前返回，未加载根配置；之前注入 DATABASE_URL 的集成测试未覆盖。已改为只读 `../.env`，保持容器和进程注入变量优先。

- Go 新增根配置读取回归测试，覆盖 backend/.env 不存在与进程变量不被覆盖；go test、go vet 和重新构建通过。
- 本机 8080（Go）、8000（RAG）、5173（Web）实际启动成功，数据库健康检查通过；服务在后台隐藏运行，不注册系统服务。
- 独立无头 Edge 使用真实本地配置账号登录，验证空知识库提示、知识管理、创建知识库表单和刷新后登录恢复，无页面脚本异常。截图为 `data/screenshots/local-ready.png`，未写入测试知识库。

这一阶段飞书 CLI 因缺少文档读取/导出 scope 暂未获取语料，页面启动验收不计入 RAG 质量结果。用户随后完成授权，语料验证见下文。

## 2026-10-10 真实语料与问答验证

用户授权从 PRD 获取材料，并完成飞书读取与导出授权。通过用户身份导出 8 份 Markdown；另一个中央机关差旅飞书链接返回文档不存在，改为获取 PRD 对应的[财政部财行〔2013〕531 号原文](https://xzzf.mof.gov.cn/zcfbyjd/201401/t20140106_1033058.htm)。2013 年和 2023 年差旅材料分别标明年份及适用组织，本次未核验其现行效力。

- 9 份文档分入 7 个适用范围明确的知识库；真实数据库确认 649 个切片，全部向量为 1024 维，导入失败任务为 0。
- 使用真实 DeepSeek/Qwen API 完成 7 个问答：交大 VPN 账号、石油大学 VPN 账号、人大移动 VPN 地址、Windows 网络重置及 QQ 冻结账号处理均返回完整答案和对应原文引用；在交大库询问另一学校配置、在 Windows 库询问公司审批人联系方式均拒答且无引用。
- 已逐项核对上述 5 个回答和引用原文，并验证所有引用归属于选定知识库。耗时为本次小样本观察，未据此计算正式正确率、召回率或延迟指标。
- 无头 Edge 验证真实账号登录、Windows 历史回答、引用展开、3 份知识文档、版本切片和刷新后登录恢复，无脚本异常；截图为 `data/screenshots/live-rag-answer.png` 和 `data/screenshots/live-knowledge.png`。
- 生成 2 条 Windows 评测候选并通过原文锚点校验，状态仍为 review、审核记录为空；没有发布数据集或运行正式质量评测。候选预览为 `eval/private/windows-ai-draft-review.json`。

真实候选生成暴露枚举字段写成自然语言、输出截断及顶层结构错误，校验器均拦截、未保存无效样本。现已提供明确的输出 Schema、按题量增长的离线输出预算以及数量/唯一 ID/证据校验；模型提供的审核记录强制清除。在线问答默认输出预算保持不变。

新增回归验证后 Python pytest 共 20 项通过；真实生成结果通过 CLI validate。语料、导出资源、数据集和运行报告均留在忽略目录内。导入来源与校验值见 `data/sources/import-manifest.json`，问答记录见 `data/live-rag-results.json`。

尚待完成：至少 30 条开发样本、200 条经人工审核的冻结测试样本、四组检索消融及正式端到端评测、文档更新维护成本测量，以及云端镜像实际启动验证。当前证据仅说明这些具体本地流程可用。

## 2026-10-10 两题冻结开发集真实离线评测

用户明确确认两条候选后，使用 CLI review 逐题写入人工审核记录并发布 `windows-dev-v1`，case_count=2、split=dev。该发布目录不可覆盖，与既有 Windows 知识快照一致；本轮代码版本为 `250976b`。原材料为三篇公开 Microsoft Support 文章，本轮只使用 Windows 库。

| 实验 | Recall@5 | MRR@5 | 执行失败 | 模型 HTTP 请求 |
| --- | --- | --- | --- | --- |
| A：BM25 | 0.5 | 0.25 | 0 | 0 |
| B：向量 | 0.5 | 0.5 | 0 | 2 |
| C：混合 | 0.5 | 0.5 | 0 | 2 |
| D：混合 + 重排 | 0.5 | 0.5 | 0 | 4 |
| D：端到端 + 逐事实评分 + Ragas | 0.5 | 0.5 | 0 | 14 |

端到端两题均完整回答，有效引用编号和回答引用覆盖均为 1.0；辅助正确率为 0.5，Ragas 忠实度为 1.0。实际答案尚无人工作出裁决，正式正确率为 null。这说明引用和忠实度通过仍可能遗漏标准答案的必要事实。2 题只用于打通和诊断流程，不能外推达到业务指标，也不能说明某检索方案总体更好。

网络题召回标准段落并通过事实检查。打印机题按冻结标准未通过：应确认已开机和连接无松动/错误，实际回答为先运行自动疑难解答。后一建议有原文支持，但与标准所指定的分步流程不同；短标准段落也未进入任何一组最终 Top-5。

诊断发现原题未限定文章或章节，不同段落存在处理顺序差异。已准备 case_2 revision=2，明确询问《修复 Windows 中找不到打印机和无法识别打印机错误》的“步骤 1：检查打印机连接”，并逐项标注开机和连接状态。修订草稿通过原文锚点校验，仍待人工审核；旧发布集、旧结果和失败分母不变。此时不根据两题结果修改线上检索，也不以新题成绩替代旧基线。

私有本地产物：

- `eval/private/releases/windows-dev-v1/`：冻结题目与内容校验值。
- `reports/windows-dev-v1-retrieval/`：共享问题的四组检索报告、清单和 A/D 对比。
- `reports/windows-dev-v1-answers/`：真实答案、引用、调用用量、辅助评分和失败记录。
- `eval/private/windows-results-human-review.json`：实际回答、评分理由及修订前后对照。
- `eval/private/windows-dev-v2-proposal.jsonl`：未发布的新修订草稿。

上述实际运行合计 22 次 HTTP 请求，无重试；用量已写入本地报告，金额未换算。检索与端到端两类命令分别限制最多 40/60 次请求。至少 30 条开发题与 200 条冻结测试题、全量人工复核及云端实际启动仍待完成。

## 复现工程联测

先安装前端、RAG 和 tools 的依赖。在分别的终端启动以下服务：

1. `cd tools; npm.cmd ci; npm.cmd run test-db`：临时内存数据库监听 55432。
2. Go：设置 `DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/postgres?sslmode=disable`、`DB_SIMPLE_PROTOCOL=true`、`INTERNAL_TOKEN=local-test-internal-token-123456789`；账号配置为 admin / test-admin-password、demo / test-user-password，再从 backend 运行 `go run .`。
3. 模型替身：`cd rag; uv run uvicorn tests.fake_models:app --host 127.0.0.1 --port 8100`。
4. RAG：同一测试 DATABASE_URL / INTERNAL_TOKEN，设置两家 Base URL 为 `http://127.0.0.1:8100`、Key 为 `contract-test`、`EMBEDDING_DIMENSION=2`、`NO_PROXY=127.0.0.1,localhost`，运行 `uv run python -m helpdesk`。
5. `cd frontend; npm.cmd run dev`。

然后在 rag 终端使用相同测试配置执行 `uv run python -m tests.integration_check`、`uv run python -m tests.evaluation_check`；在 tools 执行 `npm.cmd run test-web`。测试结果和截图保存在忽略的 `data/` 目录。脚本使用固定的本机测试端口与测试账号，请在独立测试实例中运行。

本轮截图位于 `data/screenshots/`。真实服务联调完成后，应追加真实模型协议验证、真实语料规模、数据集清单、调用用量与质量报告，不覆盖本轮工程验证记录。
