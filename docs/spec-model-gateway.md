# 三类模型统一网关规格与技术方案

## 已确认需求（2026-10-12）

- 用户确认聊天、Embedding、Rerank 三个模型统一走网关，并提供应用 Key。此前仅迁移聊天的范围由本轮要求替代。
- 逻辑基础地址 `https://localhost:9443/v1`。本机现有 SSH 隧道把 9443 转发至网关服务器 `47.113.231.252` 的回环 8443。
- 所有模型请求使用 Bearer 应用 Key 与 JSON；应用只传路由别名和业务参数，厂商选择、模型名替换、厂商 Key 和上游协议组装由网关负责。
- 聊天调用 `/chat/completions`、别名 `chat`，覆盖 JSON、SSE、多轮改写与评测。
- Embedding 调用 `/embeddings`、别名 `embedding`，传字符串数组、`dimensions: 1024`、`encoding_format: float`；不传旧 DashScope 的嵌套参数。
- Rerank 调用 `/rerank`、别名 `rerank`，扁平传 query、documents、top_n。应用已有原文，使用 `return_documents: false`，不重复请求文档。
- 用户授权完整部署数据及模型凭据传输，也授权两台云服务器间建立专用 SSH 隧道：新建仅供转发的密钥、网关添加受限公钥、部署服务器配置隧道开机启动。私钥留在部署服务器，不复制本机管理私钥。
- 需求与技术方案已人工确认；代码与测试结果须在发布修改版前交用户确认。

## 技术方案

1. `CHAT_GATEWAY_ENABLED` 和 `RETRIEVAL_GATEWAY_ENABLED` 显式控制模式，默认关闭以兼容现有环境。网关基础地址使用 `MODEL_GATEWAY_BASE_URL`；应用 Key 使用 `IT_HELPDESK_RAG_API_KEY`，仅注入 RAG，不进入源码、前端、测试数据或报告。
2. 请求别名分别使用 `GATEWAY_CHAT_MODEL=chat`、`GATEWAY_EMBEDDING_MODEL=embedding`、`GATEWAY_RERANK_MODEL=rerank`。保留实际 `EMBEDDING_MODEL=qwen3.7-text-embedding` 与 1024 维，用于文档版本及旧向量兼容校验；路由别名不覆盖已存的模型身份。网关路由到其他 Embedding 模型时，须另行确认索引兼容性。
3. 解析网关 Embedding 的 data 数组与 index，Rerank 的 results 数组；保留索引、数量、维度、有限数值和零向量检查。聊天保留 JSON 对象、SSE 完整终态和用量校验，网关模式不默认携带厂商 thinking 参数。
4. 网关缺 Key 或请求失败时明确报错，不自动直连厂商。拒绝重定向；重试、并发和调用预算沿用现有行为。
5. TLS 保持链和主机名验证。使用系统信任库，并支持 `MODEL_GATEWAY_CA_FILE` 增加可信 CA。当前 CA 从已验证的本机网关连接取得，不信任未经认证下载的证书。Docker 将公开 CA 只读挂载，`MODEL_GATEWAY_CONNECT_HOST=host.docker.internal` 仅改变连接地址，Host 与 TLS SNI 保持逻辑 localhost。
6. 云端隧道由 systemd 管理，使用专用受限公钥；监听部署服务器的回环地址及 Docker 主机网关地址，避免公网监听。应用容器配置 host-gateway 映射，云端调用不依赖 Windows 本机在线。
7. Windows 原生与 Docker 启动脚本补入应用 Key。诊断及探测按已选模式检查配置；评测清单和问答 trace 记录实际调用路由，别名不作为底层厂商型号的确认结论。

## 验证与交付

- 契约测试覆盖三类路由、应用 Key、扁平参数、1024 维、批次对齐、非法输出、失败不回退、Host/SNI、JSON 和 SSE。
- 执行 Python 回归测试、Windows 脚本解析、Compose 静态校验与镜像构建。
- 本机及云端分别执行少量真实网关联调，记录 HTTP 调用次数和用量；不得输出凭据或原始请求内容。
- 云端逐表逐文件校验完整恢复、清空目标旧登录令牌，再验证登录、历史、权限、上传及问答引用；完成容器重建、主机重启和独立恢复检查。
- 用户确认代码与测试结果后发布；最后通过 SSH Web 隧道验收页面与回答。未完成公网域名和 HTTPS 时，不标记公网验收通过。

参考：[HTTPX TLS](https://www.python-httpx.org/advanced/ssl/)、[HTTPCore SNI 扩展](https://www.encode.io/httpcore/extensions/#sni_hostname)。
