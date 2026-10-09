# 云上部署

首期支持 Linux 云服务器单机 Docker Compose，适用于求职演示。Windows 原生入口仅负责本地开发，应用代码不依赖 Windows。云端保持一个 Go 与 RAG 实例，数据库和上传文件使用持久化卷。

## 配置与启动

准备 Linux 主机及 Docker Engine / Compose v2，在独立目录检出项目。创建云端独立 `.env`，不要复制 Windows 的数据库运行目录或管理凭据：

```bash
cp .env.example .env
# 编辑 .env：随机账号密码、INTERNAL_TOKEN、POSTGRES_PASSWORD、模型 Key 和业务空间地址。
chmod 600 .env
docker compose config --quiet
docker compose up --build -d --wait --wait-timeout 180
docker compose ps
```

POSTGRES_PASSWORD 使用随机字母数字，Compose 会生成容器内数据库 URL，覆盖 Windows 的 DATABASE_URL。数据库端口默认仅发布到宿主机 127.0.0.1:5432，已有服务占用时在 `.env` 加 `POSTGRES_PORT=55433`。Go 和 RAG 不发布端口；Web 仅发布到 127.0.0.1:3000。

模型凭据也可由云端启动环境注入 `DEEPSEEK_API_KEY` 与 `DASHSCOPE_API_KEY`（兼容 `QWEN_API_KEY`）；Compose 显式传给 RAG 容器。后续修改环境变量需重建容器以生效。

Go 与 RAG 的上传卷都挂载到 `/data/uploads`，解决不同运行环境的文件路径差异；RAG 挂载只读。数据库使用 postgres 卷，原文件使用 uploads 卷。`.env` 和私有数据被构建上下文排除，不写入镜像。

服务通过健康检查等待依赖就绪，退出后由 restart 策略拉起；健康检查失败本身不会触发重启。Compose 启动顺序依据 [Docker 官方说明](https://docs.docker.com/compose/how-tos/startup-order/)。

## HTTPS 入口

将域名解析到云服务器，开放 HTTP/HTTPS 入口，使用宿主机 Caddy、Nginx 或云负载均衡转发到 127.0.0.1:3000。示例采用宿主机 Caddy：

```bash
export APP_DOMAIN=你的实际域名
caddy validate --config deploy/Caddyfile --adapter caddyfile
caddy run --config deploy/Caddyfile --adapter caddyfile
```

长期运行时由宿主机服务管理器管理 Caddy，并为该服务配置 APP_DOMAIN；上面的命令用于首次联调。域名与端口满足条件后 Caddy 管理 HTTPS，参见 [官方 HTTPS 说明](https://caddyserver.com/docs/quick-starts/https)。如果已有云负载均衡，使用其证书与入口即可。

公网经 HTTPS 访问 Web，Go API 经 Web 的 `/api/` 代理；流式问答保持关闭代理缓冲与 600 秒读取超时。云端验收包括登录、上传真实导出文件、任务完成、问答引用、刷新恢复，以及离线评测。

## 升级与持久化

升级前备份数据库和上传文件。不要把 Windows PostgreSQL 16 的物理数据目录复制给云端 PostgreSQL 17；跨版本迁移使用逻辑备份恢复。文档版本保存了绝对文件路径，首期建议云端重新上传导出文件，避免直接恢复含 Windows 路径的应用库；完整历史迁移需同步复制原文件并明确转换路径。

以下逻辑备份命令不输出数据库密码，适用于新云实例；备份包含私有语料，应存到受控位置：

```bash
mkdir -p data/backups
chmod 700 data/backups
# 维护窗口停止应用写入，保证数据库与文件备份一致。
docker compose stop web rag backend
docker compose exec -T db pg_dump -U rag -d rag -Fc > data/backups/rag.dump
docker compose run --rm --no-deps --user 0 --entrypoint tar backend -czf - -C /data uploads > data/backups/uploads.tar.gz
docker compose up -d --wait
```

完整备份时应先停止应用写入，再依次导出数据库与文件。`docker compose down` 保留命名卷；删除卷会丢失数据。更新代码后运行 `docker compose up --build -d --wait`，确认健康状态与一次业务流程。

若未来拆成多台主机，需要把原文件存储替换为对象存储，并补充分布式会话互斥、索引缓存与迁移管理。当前未实现这些扩容能力。

## 验证状态

Windows 原生 PostgreSQL 的跨服务与评测验证已通过；独立 Compose 工具的 `config --quiet` 静态校验通过，CI 已增加容器构建与健康检查任务。当前 Windows 未安装 Docker Desktop，该 CI 任务尚未运行，镜像构建、Linux 容器运行、云端 HTTPS 与备份恢复尚未实测；需要在目标 Linux 环境完成验收后再确认云部署通过。
