# 云上部署

首期支持 Linux 云服务器单机 Docker Compose，适用于求职演示。Windows 原生入口仅负责本地开发，应用代码不依赖 Windows。云端保持一个 Go 与 RAG 实例，数据库和上传文件使用持久化卷。

## 配置与启动

准备 Linux 主机及 Docker Engine / Compose v2，在独立目录检出项目。创建云端独立 `.env`，不要复制 Windows 的数据库运行目录或管理凭据：

```bash
cp .env.example .env
# 编辑 .env：随机账号密码、INTERNAL_TOKEN、POSTGRES_PASSWORD、模型 Key 和业务空间地址。
chmod 600 .env
# 使用已提交、工作区干净的代码构建，评测清单将记录这个版本。
export APP_REVISION="$(git rev-parse HEAD)"
docker compose config --quiet
docker compose up --build -d --wait --wait-timeout 180
docker compose ps
```

POSTGRES_PASSWORD 使用随机字母数字，Compose 会生成容器内数据库 URL，覆盖 Windows 的 DATABASE_URL。数据库端口默认仅发布到宿主机 127.0.0.1:5432，已有服务占用时在 `.env` 加 `POSTGRES_PORT=55533`。Go 和 RAG 不发布端口；Web 默认仅发布到 127.0.0.1:3000，可用 WEB_PORT 配置宿主机端口。

模型凭据也可由云端启动环境注入 `DEEPSEEK_API_KEY` 与 `DASHSCOPE_API_KEY`（兼容 `QWEN_API_KEY`）；Compose 显式传给 RAG 容器。后续修改环境变量需重建容器以生效。

Go 与 RAG 的上传卷都挂载到 `/data/uploads`，解决不同运行环境的文件路径差异；RAG 挂载只读。数据库使用 postgres 卷，原文件使用 uploads 卷。`.env` 和私有数据被构建上下文排除，不写入镜像。

Windows 的 `tools/docker.ps1` 使用项目名 `it-helpdesk-rag-docker` 和数据库端口 55533，保留现有 `.env`；云端不依赖该脚本或本机 Go/Node/Python 安装。每台主机保持相同项目名，避免误创建另一组空卷。生产演示使用独立账号、密码、内部令牌和模型配置。

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

### 从本地 Docker 迁到云端

本地 Docker 和云端容器的文件路径均为 `/data/uploads`，可迁移文档、向量、历史会话与引用，无需重新调用 Embedding。将数据库备份与上传文件备份作为同一批次传输，在**全新的目标实例**恢复，避免覆盖已有业务数据。Linux 目标机示例：

```bash
# 在新目录配置 .env，先构建镜像并只启动数据库。
docker compose build
docker compose up -d db --wait
# db 为新空库；不要在有业务数据的实例执行恢复。
docker compose exec -T db pg_restore --exit-on-error -U rag -d rag < data/backups/rag.dump
# 使用 Go 镜像创建上传卷及其目录，随后以 root 恢复文件和应用所有权。
docker compose run --rm --no-deps --user 0 --entrypoint sh backend -c 'tar -xzf - -C /data && chown -R 10001:10001 /data/uploads' < data/backups/uploads.tar.gz
docker compose up -d --wait
```

容器恢复后的演示账号密码会由目标 `.env` 更新；原有登录令牌随数据库恢复，正式对外提供演示前建议在维护窗口清空 tokens，要求重新登录。迁移后核对文档/切片数量、原文件校验值、历史会话与引用，再联调 HTTPS。不要只迁移数据库而遗漏文件卷。

上述二进制重定向命令用于 Linux shell。Windows 导出时使用 PowerShell 7.4+ 的原生二进制重定向，或用 Python `subprocess` 将 stdout 写入二进制文件；不要使用 Windows PowerShell 5.1 的文本重定向保存 dump/tar。Windows 脚本项目须在命令中指定 `-p it-helpdesk-rag-docker`，保证导出对应卷。

### 容器内离线评测

RAG 镜像包含 `rag-eval` 及忠实度评分依赖。私有题集、语料与报告不写入镜像，复制到运行中的容器临时目录再执行；重新创建容器前把结果导回主机：

构建版本由 APP_REVISION 写入镜像标签和评测清单，不复制 `.git`。Windows 启动脚本自动读取 HEAD，未提交代码追加 `-dirty`；直接使用 Compose 时需在构建前设置 APP_REVISION，否则报告会明确记录 unknown。Ragas 的可选 Git 实验模块在轻量镜像中静默导入，忠实度评分仍使用既有用户模型适配器。

```bash
docker compose cp eval/private rag:/tmp/eval
docker compose exec -T rag rag-eval --help
# 按 docs/evaluation.md 执行，文件参数改为 /tmp/eval 下的实际路径。
# 如指定 --out /tmp/evaluation-report，完成后导出：
docker compose cp rag:/tmp/evaluation-report reports/docker-evaluation
```

评测沿用冻结数据版本、知识库 ID 和调用预算。独立新库不自动适配原生环境的黄金集；迁移完整库后仍需核对清单。容器中的临时文件不属于数据库/上传卷备份，需另行保存。

若未来拆成多台主机，需要把原文件存储替换为对象存储，并补充分布式会话互斥、索引缓存与迁移管理。当前未实现这些扩容能力。

## 验证状态

2026-10-10 已在 Windows Docker Desktop 的 Linux 引擎完成三应用镜像构建、四服务健康检查、真实文档导入/问答与网页验证；删除并重建容器后数据保留，独立实例的数据库与上传文件备份恢复通过。容器内评测 CLI、Ragas 评分适配器及镜像代码版本标识验证通过。该验证说明单机 Linux 容器方案可运行；实际云服务器部署、域名/证书、HTTPS 和公网访问尚未实测。CI 已配置同类构建任务，本轮未执行 GitHub Actions。
