# 知答 · IT 知识助手

Python 知识问答应用：FastAPI 业务 API、LangGraph 问答编排、独立 Python Worker、Vue 3/TypeScript、PostgreSQL/pgvector。在线应用统一在 `rewrite/`，已移除 Go 服务、旧前端和内部转发链。

保留登录、用户组与文档权限、知识库与版本管理、文件上传和异步入库、多库问答、POST SSE、引用原文下载、会话历史、幂等和撤权中止。当前运行基线策略；覆盖补查和独立回答检查仍按 [原规格](docs/redesign/spec.md) 后续推进。

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
