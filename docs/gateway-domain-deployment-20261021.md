# 2026-10-21 正式模型网关域名切换记录

状态：[需求](spec-gateway-domain-20261021.md) 与 [技术方案](technical-plan-gateway-domain-20261021.md) 已人工确认，生产配置切换与自动验证完成；真实问答待用户人工验收。

## 实际变更

服务器为 `8.155.133.72`，正式 Compose 项目为 `it-helpdesk-python`。只修改 `/opt/it-helpdesk-rag-python-20261018/source/.env.python` 的五项：

```dotenv
HD_CHAT_URL=https://api.linhengyi.xyz/v1/chat/completions
HD_EMBEDDING_URL=https://api.linhengyi.xyz/v1/embeddings
HD_RERANK_URL=https://api.linhengyi.xyz/v1/rerank
HD_MODEL_CONNECT_HOST=
HD_MODEL_CA_FILE=
```

新域名指向原网关服务器 `47.113.231.252`。正式 API、Worker 直接连接域名的 HTTPS 443，不再通过旧 `localhost:9443` 路径。实际 Settings 的连接覆盖为空，附加 CA 为 None；应用仍使用系统可信证书库校验证书和域名，不接受重定向。

旧配置文件已在服务器 `/opt/it-helpdesk-gateway-domain-20261021/backup/.env.python` 备份，目录权限 700、文件权限 600。完整配置和备份没有传回本机或提交 Git。

## 切换范围与过程

1. 两次确认在途任务为零：正在生成的回答 0、排队或执行中的入库任务 0。先正常停止 API，再复查任务，随后停止 Worker。
2. 替换五项配置，核对其余文件行不变。完整 Compose 展开后，两服务的新值均正确，其他模型设置保持一致。
3. 使用原镜像重建 API、Worker。API healthy、Worker running；Web 内 `nginx -t` 与重载通过，用于重新解析 API 地址。
4. 从发起停止 API 到公网健康检查恢复的服务操作窗口为 **51.524 秒**。
5. 逐项比较重建前后的容器环境变量：仅上述五项改变，通用及三类 Key、模型身份、别名、维度、调用限制和推理开关全部保持原值。比较只输出布尔结论，未记录凭据。

API、Worker 的镜像 ID 仍为 `sha256:1845441eed05dcba6287e8dc20dfabc2525ff15b72508b0759927913a10af8ad`，镜像标签 `helpdesk-python:179ac07`。没有修改应用代码或构建新镜像。Web 仍为 `helpdesk-python-web:13f1e9c`，Web 与数据库容器 ID 和镜像 ID 均保持不变。

生产命令始终携带 `compose.yaml`、`compose.server.yaml`、`compose.public-https.yaml`。Caddy、数据库、候选环境及本机开发配置不操作；共享 `it-helpdesk-gateway-tunnel` 仍为 active，旧私有 CA 文件保留，供其他环境和回退使用。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| 配置差异 | 文件和实际容器环境均只改变五项；其他变量及凭据一致 |
| 配置解析 | 完整 Compose 配置校验通过，API、Worker 读取的实际 Settings 正确 |
| 网关认证 | 两个重建容器分别使用现有三类 Key，经应用实际 HTTP 客户端执行 6 次 `GET /v1/models`，全部返回 200；每次均包含 `chat`、`embedding`、`rerank` 预期别名 |
| 证书与路径 | 系统证书校验通过；实际请求目标为 `api.linhengyi.xyz` 默认 443，无连接主机覆盖 |
| 服务健康 | API healthy、Worker running，公网 `/health` 为 200 且 `chat_ready=true`，首页为 200 |
| 公网浏览器 | 本机 Edge 9 项检查通过、页面错误 0；覆盖可信 HTTPS、HTTP 跳转、桌面/手机登录前后备案、已有账号登录、历史/引用读取、授权文件下载、未登录拒绝与退出失效 |
| 数据与模型调用 | 不迁移数据库，不重建向量，不新建问答或上传文档；代理自动验证的付费推理调用为 **0** |
| 回退 | 本次未触发；完整旧配置和旧隧道/证书仍保留 |

模型元数据 GET 证明新域名的连接、证书与应用认证正常，真实推理输出、流式回答和模型质量仍待用户在 [正式站点](https://helpdesk-rag.linhengyi.xyz) 使用原账号人工验收。没有重跑效果评测或重复整套未受影响的业务回归。

## 维护、回退与证据

维护命令与配置片段见 [模型网关维护](model-gateway-domain.md)。失败时从服务器受控备份恢复原 `.env.python`，用全部生效 Compose 文件及原镜像恢复 API、Worker，再校验并重载 Web nginx。恢复仅涉及配置和进程，不恢复数据库快照。

服务器目录 `/opt/it-helpdesk-gateway-domain-20261021/artifacts/` 保存脱敏变更前身份、切换结果和公网浏览器验证摘要；本机忽略目录 `data/gateway-domain-20261021/` 留存筛选后的证据副本。证据不包含 Key、登录令牌、历史正文或下载文件内容。

对应记录在 `codex/gateway-domain-20261021` 分支提交；没有推送或合并 PR。
