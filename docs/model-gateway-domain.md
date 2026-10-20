# 正式模型网关配置与维护

2026-10-21 已完成正式 API、Worker 的域名切换，实际验证与回退材料见 [切换记录](gateway-domain-deployment-20261021.md)。当前正式网关为 `https://api.linhengyi.xyz/v1`。应用需要完整端点；在生产目录 `/opt/it-helpdesk-rag-python-20261018/source/.env.python` 中合并以下五项，不覆盖其他配置或应用 Key：

```dotenv
HD_CHAT_URL=https://api.linhengyi.xyz/v1/chat/completions
HD_EMBEDDING_URL=https://api.linhengyi.xyz/v1/embeddings
HD_RERANK_URL=https://api.linhengyi.xyz/v1/rerank
HD_MODEL_CONNECT_HOST=
HD_MODEL_CA_FILE=
```

空连接覆盖表示直接解析网关域名；空附加 CA 表示使用系统可信证书库。继续保留证书及主机名验证，不接受自动重定向。现有三个请求别名为 `chat`、`embedding`、`rerank`；实际模型身份及 1024 维向量与旧配置一致。

## 配置生效

正式配置修改前，在服务器受限目录保存权限为 600 的原文件并核对无在途任务。配置及备份不复制到本机或 Git。仅修改文件或容器 restart 不会更新已创建容器的环境，需在维护窗口重建 API、Worker；数据库、Web 镜像和 Caddy 沿用原版本。

所有维护命令保留三份生效配置，避免 Web 被恢复为旧镜像：

```sh
cd /opt/it-helpdesk-rag-python-20261018/source
docker compose --env-file .env.python -p it-helpdesk-python -f compose.yaml -f compose.server.yaml -f compose.public-https.yaml config --quiet
docker compose --env-file .env.python -p it-helpdesk-python -f compose.yaml -f compose.server.yaml -f compose.public-https.yaml up --no-build --no-deps -d --wait api worker
docker exec it-helpdesk-python-web-1 nginx -t
docker exec it-helpdesk-python-web-1 nginx -s reload
```

重载 nginx 用于重新解析重建后的 API 地址。不要输出完整 Compose 配置或容器环境；检查只显示端点等非敏感字段，凭据一致性仅输出布尔结论。

## 验证与回退

重建后检查 API healthy、Worker running；在两个容器内部读取实际 Settings，确认三类端点和空连接覆盖/附加 CA 已生效。使用应用自身 HTTP 客户端和既有 Key 对新网关执行 `GET /v1/models`，验证证书和预期别名。该检查不执行付费推理，不能代替真实问答验收。

同时验证公网 `/health`、登录、历史读取和退出；核对服务镜像不变、Web 与数据库容器未重建。真实问答由用户人工验收。

失败时恢复服务器原配置备份，以相同镜像和全部 Compose 文件恢复 API、Worker，重载 nginx 并核对健康。保留旧隧道与私有 CA 文件以供其他环境和回退使用；不还原业务数据库快照。
