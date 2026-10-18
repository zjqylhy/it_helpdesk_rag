# Python 单机部署与恢复

服务为一个 Python API 进程、独立 Python Worker、Vue/nginx 和 PostgreSQL/pgvector。API 通过数据库锁拒绝第二进程；权限屏障依赖单 API 进程，不增加 Uvicorn workers 或 API 副本。

## 新环境启动

Linux 独立目录检出已提交版本；复制 `.env.example` 为 `.env.python`，将两处 `CHANGE_ME` 替换为同一随机字母数字密码，填写模型配置并限制文件权限：

```sh
chmod 600 .env.python
export APP_REVISION="$(git rev-parse HEAD)"
docker compose --env-file .env.python -p it-helpdesk-python config --quiet
docker compose --env-file .env.python -p it-helpdesk-python build api web
docker compose --env-file .env.python -p it-helpdesk-python up -d --wait db
docker compose --env-file .env.python -p it-helpdesk-python run --rm --no-deps api python -m helpdesk.entrypoints.migrate --directory /workspace/rewrite/db/migrations
docker compose --env-file .env.python -p it-helpdesk-python run --rm --no-deps api python -m helpdesk.entrypoints.admin admin
docker compose --env-file .env.python -p it-helpdesk-python up --no-build -d --wait
```

管理员密码交互输入，只能在空账号库引导。既有账号库跳过此步。正式部署使用唯一项目名且后续保持一致；新卷 `python_database`、`python_files` 与历史应用隔离。网页默认回环 3000，数据库回环 55534；外部 HTTPS 由宿主机 Caddy/nginx 转发，示例见 `deploy/Caddyfile`。

模型端点和凭据通过 `.env.python` 注入；私有 CA 放 `deploy/certs/`，容器路径为 `/run/certs/文件名`。宿主机隧道使用 `HD_MODEL_CONNECT_HOST=host.docker.internal`，保留逻辑 Host/SNI 和 TLS 校验。配置完整后显式开启 `HD_MODEL_INFERENCE_ENABLED=true`。旧内部令牌与服务转发地址不再使用。

## 旧数据迁移

先停止旧写入者，备份原数据库和上传文件；在独立实例恢复副本，记录表数量、主键、活动版本、文档/切片哈希、向量维度与模型身份、授权状态和文件哈希。仅对副本执行迁移。`helpdesk-map-files` 使用显式版本到原文件的映射和内容哈希复制，不猜测旧绝对路径；命令参数见 `python -m helpdesk.entrypoints.map_files --help`。

新旧服务不能同时写同一套任务。切换前验证历史会话、引用与权限、文件下载、新版失败保留旧版和新上传发布；不匹配的向量身份应停止并核对，不能静默重新向量化。已恢复的历史令牌应清空后重新登录。生产迁移与入口切换单独验收。

## 备份、升级和恢复

先停 API 与 Worker，保留数据库运行，使用 PostgreSQL `pg_dump -Fc` 导出应用库，并单独归档文件卷；备份文件和配置保存在仓库之外的受控位置。记录当时 commit、模型身份与文件哈希。二进制 dump 使用文件或容器内输出后 `docker cp` 复制，Windows PowerShell 5.1 不通过文本重定向保存二进制。

升级顺序：备份 → 停写入 → 构建目标版本 → 显式迁移 → 启动 API/Worker/Web → 回归验证。API 正在运行时迁移会失败，不绕过锁。

恢复时创建新的隔离项目和卷，先用 `pg_restore` 还原备份，再还原对应文件归档并核对哈希；用同一备份时刻的代码和配置验证。向前迁移不承诺旧代码兼容，回滚使用完整备份及对应版本，不能直接删除字段或覆盖历史答案。`docker compose ... down` 保留卷；删除卷不属于常规停止步骤。

## 源码包

`python rewrite/scripts/package_deployment.py NEW.tar.gz` 打包 Python/Vue、迁移、根 Compose、配置模板和规格，含逐文件 SHA256 与基准提交；排除凭据、证书和业务数据。构建使用 `rewrite/deploy/Dockerfile`。旧的 `rewrite/deploy/compose.yaml` 预览入口已统一为根目录配置。
