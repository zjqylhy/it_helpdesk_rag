# 聊天模型网关改造规格与技术方案

## 已确认需求

- 用户要求先仅迁移聊天应用；Embedding 与重排保持原有阿里接口与凭据。
- 聊天入口为 `https://localhost:8443/v1/chat/completions`，本机 SSH 隧道转发至服务器网关。
- 请求采用 Bearer 应用 Key、JSON 消息数组、模型别名 `chat` 和 `max_tokens`；覆盖结构化生成、在线流式回答及评测中的聊天调用。
- Key 使用 `IT_HELPDESK_RAG_API_KEY`，已按用户授权保存到 Windows 用户环境变量。不写入 Git、前端、测试数据或文档。
- 技术方案待用户确认；应用代码尚未修改。新分支为 `codex/model-gateway`。

## 技术方案

1. 新增明确的聊天网关配置开关、带 `/v1` 的网关基础地址及聊天模型别名。Key 只读取 `IT_HELPDESK_RAG_API_KEY`。网关模式缺少 Key 或请求失败时返回错误，不自动回退直连 DeepSeek。
2. 将现有聊天 JSON 和流式请求统一绑定到选定的聊天客户端与路由；网关使用 `chat` 别名，保留结构化响应与流式终态校验。网关请求不默认携带厂商特有的 `thinking` 参数；通用 JSON、流式、用量参数须在实测中验证。
3. Embedding 和重排使用原有客户端、模型及配置。现有向量、文档版本和知识快照无需重建，不因聊天别名修改 Embedding 模型身份。
4. TLS 保持校验。Windows 原生模式使用系统可信证书；容器使用显式配置的可信 CA 文件。可配置网关连接主机，供 Docker 使用 `host.docker.internal` 访问宿主机隧道，同时保持逻辑地址 `localhost` 的 Host、TLS 服务器名和证书校验。
5. Windows 原生与 Docker 启动脚本补入用户环境变量；Compose 只将网关 Key 注入 RAG。隧道与 CA 配置分别说明，不把 Key 写到 `.env.example` 或镜像。
6. 诊断与模型探测按所选聊天模式检查凭据；评测清单记录网关路由和实际请求别名，不将网关别名声称为已确认的底层厂商模型。

## 预检查结果（2026-10-11）

- 已建立独立分支，Key 已保存，未输出值。
- 按用户示例发送一次 `max_tokens=64` 的短聊天请求，得到 HTTP 401；只读模型列表同样得到 HTTP 401。实际接口鉴权尚未通过，需核对应用 Key 或网关鉴权配置。
- Python 默认 certifi 验证失败；使用系统证书库时连接成功并收到 HTTP 401。未关闭证书校验。
- 现有 RAG 容器到 `host.docker.internal:8443` 的 TCP 连接成功；容器内完整 TLS/鉴权尚未验证。

## 验证与交付

- 契约测试：聊天 JSON 与 SSE 只请求网关路由、使用应用 Key 和 `chat`；网关失败不直连回退；Embedding/重排维持原协议。
- 回归测试：Python 测试、配置检查、Windows PowerShell 5.1/7 脚本解析、Compose 静态校验。
- 实际联调：修复 HTTP 401 后验证聊天 JSON、SSE 完整终态和用量，验证 Docker 的宿主机隧道及证书配置。
- 页面验收：真实知识问答、引用、学校澄清承接和历史恢复；记录通过结果与仍待执行项。
- 应用代码修改验证后提交，再由用户确认代码与测试结果。真实联调未通过时不宣称流量已经成功切换。

参考：[HTTPX TLS](https://www.python-httpx.org/advanced/ssl/)、[Docker Desktop 网络](https://docs.docker.com/desktop/features/networking/)。
