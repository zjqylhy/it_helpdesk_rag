# 知答 · 推送知识库查询助手

当前演示语料为推送接入、指标口径、内容审核和排查经验，面向同组研发及相关业务方同事。语料位于 [knowledge/push](knowledge/push/README.md)，共 9 个主题、18 篇文档，来自原创模拟手册及官方公开资料的简短转述，不包含公司内部原文。应用架构保持原有实现；历史 IT 语料的离线评测结果不代表本套推送语料的效果。

在线身份、范围判断、多轮改写、证据判断、回答及页面统一采用推送场景表述，缺条件时提示补充应用、平台/通道、时间和异常现象。存储与接口保留 `organization` 兼容字段，界面含义为“适用业务范围”，不代表阅读权限。范围过大时提示缩小知识主题。实现与验证见 [口径统一记录](docs/push-domain-unification.md)。

评测术语：历史 `dev` 是用于比较提示词与检索方案的“方案对比评测集”，`test` 是保留的“独立验证集”，均不表示模型训练或权重调参。当前推送 12 题是“功能验收样例”；协议替身案例属于自动化测试。各类结果分别报告，不混称为模型准确率。

Python 知识问答应用：FastAPI 业务 API、LangGraph 问答编排、独立 Python Worker、Vue 3/TypeScript、PostgreSQL/pgvector。在线应用统一在 `rewrite/`，已移除 Go 服务、旧前端和内部转发链。

保留登录、用户组与文档权限、知识库与版本管理、文件上传和异步入库、多库问答、引用原文下载、会话历史、幂等和撤权中止。问答由 API 内异步任务执行，页面每秒查询进度；切页、刷新、断网或关闭标签页不取消已受理任务，返回后继续查看原消息。主动停止、权限失效、超时及服务关闭仍会终止任务；API 重启不自动续跑。当前运行基线策略；覆盖补查和独立回答检查仍按 [原规格](docs/redesign/spec.md) 后续推进。

断连恢复的实现、接口切换与验证结果见 [交付记录](docs/chat-reconnect-implementation.md)；已部署正式服务器，发布与回退说明见 [部署记录](docs/chat-reconnect-deployment-20261023.md)，页面体验待人工验收。

## Windows 开发

需要 Python 3.12、uv、Node.js 和 PostgreSQL/pgvector。根目录执行：

```powershell
.\tools\windows.ps1 -Action Init
.\tools\windows.ps1 -Action Setup
# 编辑 .env.python，配置独立数据库和文件根。
.\tools\windows.ps1 -Action Migrate
.\tools\windows.ps1 -Action Bootstrap -Username admin
```

分别在三个终端启动：

```powershell
.\tools\windows.ps1 -Action Run -Service Api
.\tools\windows.ps1 -Action Run -Service Worker
.\tools\windows.ps1 -Action Run -Service Web
```

页面地址 [http://127.0.0.1:13000](http://127.0.0.1:13000)，API 默认 18080。启动不会自动迁移或重置账号。详见 [Windows 说明](docs/windows.md)。

## Docker Compose

```powershell
.\tools\windows.ps1 -Action Init  # 已有 .env.python 时跳过
.\tools\docker.ps1 -Action Check
.\tools\docker.ps1 -Action Build
.\tools\docker.ps1 -Action Migrate
.\tools\docker.ps1 -Action Bootstrap -Username admin
.\tools\docker.ps1 -Action Start
```

页面地址 [http://localhost:3000](http://localhost:3000)，数据库默认绑定本机 55534。项目名 `it-helpdesk-python`，使用独立数据库和文件卷；Stop 保留卷。配置文件为 `.env.python`，旧 `.env` 和旧业务数据不会自动迁入。Linux 命令、备份和升级见 [部署说明](docs/cloud.md)。

模型未完整配置或 `HD_MODEL_INFERENCE_ENABLED=false` 时，页面与管理功能可用，问答返回 503，Worker 不领取任务。配置完整端点、实际模型名和密钥后显式开启；请求别名用 `HD_*_REQUEST_MODEL`，与实际模型身份区分。私有 CA 见 [证书目录](deploy/certs/README.md)。

## 验证与评测

```powershell
# 指定独立 PostgreSQL 管理连接，测试会创建并销毁合成数据库。
$env:HD_TEST_DATABASE_URL = 'postgresql://USER:PASSWORD@127.0.0.1:PORT/postgres'
.\tools\windows.ps1 -Action Verify -Browser
```

验证覆盖 Python Ruff/mypy/pytest、Vue 协议/类型/构建、真实 API + Worker + 浏览器；模型请求仅访问本机替身。CI 同时执行根目录容器启动验证。

新评测使用 `helpdesk-eval`，先执行 `uv run --env-file .env.python --project rewrite/backend helpdesk-eval --help`。旧评测集维护、Ragas 和历史重放继续使用 [独立离线评测环境](rag/README.md)，旧结果及裁决保留。历史成绩见 [原验证记录](docs/verification.md)，不能作为新应用的质量成绩。

[迁移范围与本次验证](docs/python-unification.md) · [原规格与设计](docs/redesign/README.md)

2026-10-18 已将服务器主入口切换为 Python，原账号与知识/历史保留，详见 [实际部署记录](docs/python-deployment-20261018.md)。

2026-10-21 已接入 [正式 HTTPS 站点](https://helpdesk-rag.linhengyi.xyz) 并展示 ICP 备案号。该次发布核验公网登录、历史与引用下载；后续推送语料真实问答样例单列于 [推送发布记录](docs/push-knowledge-deployment-20261024.md)，不将公网可用性当作答案质量。维护方式见 [公网发布记录](docs/public-https-deployment-20261021.md) 和 [维护说明](docs/public-https.md)。

正式模型网关已统一切换到 `https://api.linhengyi.xyz/v1`；API、Worker 的实际客户端认证访问及公网基础业务验证通过，付费推理调用为 0。详见 [网关切换记录](docs/gateway-domain-deployment-20261021.md)。
