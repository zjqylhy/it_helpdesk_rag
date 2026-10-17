# 独立服务器验收部署

此配置启动一个 API 进程、独立 Worker、Vue/nginx 和 PostgreSQL/pgvector。必须使用独立项目名、数据库和卷，不覆盖旧项目。

## 构建与配置

从仓库根执行 `python rewrite/scripts/package_deployment.py NEW.tar.gz` 生成仅包含新实现和规格的源码包；包中有逐文件 SHA256、基准提交和工作区是否干净的记录。它不包含 `.env`、旧 Go/RAG、原始素材、上传文件或私有评测集。上传后逐项校验清单，不能把候选包的基准提交误认为候选内容已经提交。

在服务器 `rewrite/deploy/.env` 显式配置（文件权限 600）：

```dotenv
APP_REVISION=YOUR_VERIFIED_REVISION
POSTGRES_USER=YOUR_ISOLATED_USER
POSTGRES_PASSWORD=YOUR_RANDOM_PASSWORD
POSTGRES_DB=helpdesk_preview
HD_DATABASE_URL=postgresql://YOUR_ISOLATED_USER:YOUR_RANDOM_PASSWORD@db:5432/helpdesk_preview
WEB_PORT=3001
GATEWAY_CA_FILE=/absolute/path/to/your/gateway-ca.pem
HD_MODEL_CA_FILE=/run/certs/gateway.pem
HD_MODEL_INFERENCE_ENABLED=false
```

网关模型信息按 `Settings` 的 `HD_CHAT_*`、`HD_EMBEDDING_*`、`HD_RERANK_*` 显式配置。`*_MODEL` 记录实际模型身份，`*_REQUEST_MODEL` 记录网关别名，两者不可混用。`HD_MODEL_CONNECT_HOST=host.docker.internal` 可连接宿主机隧道，同时保持 URL 中的 Host 与 TLS SNI。不允许关闭证书验证。密钥只经服务器私有 `.env` 注入，不使用镜像构建参数。

标准构建与启动（在新源码包根目录）：

```sh
docker compose -p helpdesk-preview -f rewrite/deploy/compose.yaml build
docker compose -p helpdesk-preview -f rewrite/deploy/compose.yaml up -d db
docker compose -p helpdesk-preview -f rewrite/deploy/compose.yaml run --rm api python -m helpdesk.entrypoints.migrate --directory /workspace/rewrite/db/migrations
docker compose -p helpdesk-preview -f rewrite/deploy/compose.yaml up -d --no-build api worker web
```

旧数据须先只读备份并恢复到新库，再显式迁移；API 启动期间迁移被互斥锁拒绝。旧文件用 `helpdesk-map-files` 显式映射并核对内容哈希，不能直接猜测历史绝对路径或让新应用写原上传卷。副本中的旧会话令牌应清空后重新登录。

基础镜像版本可以通过 Dockerfile 的 `PYTHON_BASE`、`NODE_BASE`、`NGINX_BASE` 参数指定；有条件时应锁定已验证镜像 digest。下载源可用 `PACKAGE_INDEX` 和 `PACKAGE_ARTIFACT_ROOT` 配置：仅替换工件下载前缀，保留版本和 SHA256，完成后恢复镜像中的原始 `uv.lock`。下载源必须由部署者信任，不允许依赖解析结果自动漂移。

## 服务器验收

构建 `verify` target 运行 Python 集成测试、Ruff 和 mypy；前端构建阶段已经执行 npm 协议测试及 TypeScript/Vite。QA 容器只注入独立数据库的 `HD_TEST_DATABASE_URL`，不注入真实模型 Key。pytest 为每例创建并销毁独立合成数据库。

构建 `browser` target 后运行 `python /workspace/rewrite/scripts/verify_browser.py`，脚本在 QA 容器内启动 API、Worker、Vue 和 HTTP 模型替身。产物可挂载到 `/workspace/data/rewrite-m1-qa`；不得把它当作真实模型质量或性能结论。

候选环境 `HD_MODEL_INFERENCE_ENABLED=false` 时，`/health` 的 `chat_ready` 为 false，问答请求返回 503，Worker 不领任务，HTTP 模型适配器也会阻止推理。只读 `/models` 连通性不等于聊天、向量和重排的推理协议已经联调。

web 仅绑定服务器回环端口。如需访问，用 SSH 本地转发；正式公网入口切换不在验收命令之内。停止验收仅对选定的新项目执行 `docker compose ... down`，保留卷和备份；生产恢复演练及删卷需单独制定范围。
