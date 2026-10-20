# 2026-10-21 公网 HTTPS 发布记录

状态：需求与技术方案已人工确认；代码、部署和自动验证完成，等待用户通过正式域名验收真实问答。

## 实际入口与改动

- 正式入口：[https://helpdesk-rag.linhengyi.xyz](https://helpdesk-rag.linhengyi.xyz)，A 记录为 `8.155.133.72`；公共解析器复核一致，未返回 AAAA 地址。
- HTTP 返回 308，跳转到同域名 HTTPS；外部 HTTPS 页面、静态资源及 `/health` 正常，`chat_ready=true`。
- 登录前后共用备案页脚，展示 `粤ICP备2026161349号`，链接到工信部备案查询。桌面与手机页面经本机 Edge 浏览器检查和截图目视核对，中文显示正常。
- 新增宿主机 Caddy `v2.11.7` 系统服务，以 `caddy` 用户运行，已 active/enabled。TCP 80/443 监听正常，管理端口仅 `127.0.0.1:2019`。
- 只替换正式 Web，API、Worker 和数据库的容器 ID、镜像 ID 与发布前完全相同。数据库、候选 Web、模型网关的原绑定范围保持不变。
- 公网 80/443 实测可达；本轮未修改云安全组或宿主机防火墙规则。现有云 CLI 无配置，未宣称已读取完整安全组规则。

## 版本与证书

| 项目 | 实际值 |
| --- | --- |
| 页脚代码提交 | `3f7480a` |
| 发布源码和代理配置提交 | `13f1e9c9bb34bc33d2780d24eaa6b5792c52e5a4` |
| 发布包 | 24 个源码、配置及说明文件；SHA256 `022d50f771e894119299f1f343de312096da9b45577ca7f72551bcc34532ea8b` |
| 新 Web 镜像 | `helpdesk-python-web:13f1e9c` |
| 新 Web 镜像 ID | `sha256:4335da085279c0a3dae317bea1c2c0e57f28640e3453dd423aa1956de3b6cf2c` |
| 保留后端镜像 | `helpdesk-python:179ac07` |
| 旧 Web 回退镜像 | `helpdesk-python-web:179ac07`，`sha256:e71bbcb741fc245051dfd733dcce1fe62c45699d88409e8adca277c307141400` |
| Caddy 发布包 SHA256 | `727b91701a392de6ebc5027509f548bf39979e5216340d0faed8fa5e69c84f8b` |
| Caddy 可执行文件 SHA256 | `678ade3bfc088749c81a681adc603333ee0bb023b6a6cfe3c0f58bef8ff854e9` |
| 公网证书签发者 | Let's Encrypt YE2，SAN 匹配正式域名，TLS 1.3 校验通过 |
| 证书有效期（北京时间） | 2026-10-21 04:30:45 至 2027-01-19 04:30:44 |
| 证书 SHA256 指纹 | `e754ed5ebd13e78a0d6cb00fcc8c9803d8a4f017294981483d9aecd68918a0d5` |

Caddy 发布包按官方 checksums 文件中的 **SHA-512** 校验通过，并与 GitHub 发布元数据中的 SHA256 一致。新 Web 使用既有已验证 Node/nginx 基础镜像构建；复用前证明依赖锁文件仅有 CRLF/LF 差异，内容一致。对新前端源码重新执行测试、类型检查及构建，再复制新产物并覆盖镜像 revision，不复用旧页面产物。

## 验证结果

| 验证 | 结果 |
| --- | --- |
| 前端测试与构建 | 本机及服务器均通过 9 项 SSE 协议测试、TypeScript 类型检查和 Vite 构建 |
| 隔离浏览器回归 | 内部专用网络、一次性 PostgreSQL、模拟模型：18 项检查通过，页面错误 0；覆盖备案展示、桌面/手机、登录、权限、历史、文件、流式回答与取消 |
| 真实代理链路 | Caddy → 发布版 nginx → 隔离模拟事件源：5 段分别在约 0、0.249、0.499、0.750、1.000 秒到达；客户端取消传递到上游 |
| 公网浏览器 | 本机 Edge 直连：9 项检查通过，页面错误 0；可信 HTTPS、HTTP 跳转、桌面/手机登录前后备案号、既有账号登录、历史/引用、授权下载、未登录拒绝、退出后令牌失效 |
| 服务配置 | Caddy 配置以实际服务用户校验通过，systemd 服务文件校验通过 |
| 代理重启 | 重启 Caddy 后 active/enabled，HTTPS `/health` 200；后端容器保持原 ID 和镜像 |
| 付费模型调用 | **0**；未发送真实问题、上传生产文档或执行效果评测 |

生产验收使用已有账号，未重置密码。退出验证后已撤销测试登录会话；凭据、令牌、历史正文和下载文件未写入公开报告。服务器隔离测试容器和专用网络、临时预览 Web、临时代理探针均已清理。

## 生效配置与维护

- 本轮发布目录：`/opt/it-helpdesk-public-https-20261021/`；源码在 `source/`，部署及验证证据在 `artifacts/`。
- Caddy 程序 `/usr/bin/caddy`，配置 `/etc/caddy/Caddyfile`，域名环境 `/etc/caddy/helpdesk.env`，服务 `/etc/systemd/system/caddy.service`，持久化状态 `/var/lib/caddy/`。
- 正式 Compose 基础目录仍为 `/opt/it-helpdesk-rag-python-20261018/source`，新增 `compose.public-https.yaml` 仅覆盖 Web 镜像。当前维护命令必须带第三份覆盖文件：

```sh
cd /opt/it-helpdesk-rag-python-20261018/source
docker compose --env-file .env.python -p it-helpdesk-python -f compose.yaml -f compose.server.yaml -f compose.public-https.yaml ps
```

后续更新 Web 同样携带全部配置，再执行 `up --no-build --no-deps -d --wait web`。不要直接执行遗漏第三份覆盖文件的旧命令，以免将 Web 恢复为旧镜像。

需要回退 Web 时，先记录当前配置，将 `compose.public-https.yaml` 中的 Web 镜像恢复为上表的旧 Web，再仅重建 Web 并检查健康。API、Worker、数据库、卷不操作。代理失败按 [维护说明](public-https.md) 恢复配置或停止新增服务，通过原 SSH 隧道管理；不删除证书目录。

## 证据与验收限制

服务器归档 `artifacts/delivery-evidence.zip` 包含 11 份经过筛选的源码清单、镜像、证书和验证摘要。本机副本位于 Git 忽略目录 `data/public-https-20261021/`，公网登录截图亦只保存在该目录，未提交账号信息或私有业务内容。

本轮未重启整个 ECS，也未跨证书到期周期实测自动续期；已验证服务开机启用配置、持久化存储和当前证书有效。真实公网模型问答尚待用户用原账号人工验收，不以模拟流式测试替代。代码与记录已在隔离分支提交，未推送或合并 PR。
