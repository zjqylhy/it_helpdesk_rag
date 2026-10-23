# 公网 HTTPS 维护

站点为 `https://helpdesk-rag.linhengyi.xyz`，目标 ECS 为 `8.155.133.72`。Caddy 在宿主机接收 TCP 80/443，转发到已有 Web `127.0.0.1:3000`。备案号由前端共享页脚显示为 `粤ICP备2026161349号`，查询链接为 `https://beian.miit.gov.cn/`。

## 首次安装

先核对公网 A/AAAA、安全组入站 TCP 80/443、宿主机端口占用和应用 `/health`。本次采用 Caddy `v2.11.7` 官方 Linux amd64 发布包，与同版本官方 checksums 文件校验；归档、校验和及安装结果保留在服务器发布目录。安装之前记录版本，不执行不带版本的自动升级。

将校验后的 `caddy` 可执行文件安装到 `/usr/bin/caddy`。创建专用系统用户与组 `caddy`，家目录 `/var/lib/caddy`，禁止交互登录。配置目录 `/etc/caddy` 可供该用户读取；证书目录 `/var/lib/caddy` 归该用户管理，权限 700。

从已提交的发布源码安装配置与服务模板：

```sh
install -o root -g caddy -m 640 deploy/Caddyfile /etc/caddy/Caddyfile
install -o root -g caddy -m 640 deploy/helpdesk.env.example /etc/caddy/helpdesk.env
install -o root -g root -m 644 deploy/caddy.service /etc/systemd/system/caddy.service
APP_DOMAIN=helpdesk-rag.linhengyi.xyz caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemd-analyze verify /etc/systemd/system/caddy.service
systemctl daemon-reload
systemctl enable --now caddy
```

这些命令由有管理权限的部署者执行。模板中的进程以 `caddy` 用户运行，只有绑定低端口的能力。证书数据由 `XDG_DATA_HOME=/var/lib/caddy` 持久保存；不删除证书目录，也不将私钥提交 Git。

## 日常检查与修改

```sh
systemctl is-active caddy
systemctl is-enabled caddy
journalctl -u caddy --since '30 minutes ago' --no-pager
curl --fail --silent --show-error https://helpdesk-rag.linhengyi.xyz/health
curl --head http://helpdesk-rag.linhengyi.xyz/
```

域名和证书必须正常验证，不使用 `curl -k`。域名环境在系统服务中加载，交互执行校验时仍需显式提供 `APP_DOMAIN`。修改配置先备份并校验，再 `systemctl reload caddy`；修改服务文件先执行 `systemd-analyze verify`，再 `systemctl daemon-reload`。变更环境文件后需重启服务加载新环境。

Caddy 自动续期依赖持续有效的 DNS、服务器出网、校验端口和可写证书目录。检查 active/enabled 与有效证书只能证明当前可用及配置存在，不能代替跨到期时间的续期实测。

## Web 独立发布与回退

首次公网 HTTPS 发布只更新前端镜像；2026-10-23 已协调更新 API、Worker 和 Web，当前镜像及回退说明见 [问答恢复部署记录](chat-reconnect-deployment-20261023.md)。生产基础文件仍在 `/opt/it-helpdesk-rag-python-20261018/source`，Compose 项目固定为 `it-helpdesk-python`。维护命令须按顺序携带 `compose.yaml`、`compose.server.yaml`、`compose.public-https.yaml`、`compose.chat-reconnect.yaml`，不能只用基础文件启动而恢复旧镜像。

更新前记录所有正式容器 ID、运行镜像及原覆盖文件；新镜像先在仅回环的预览端口校验，再使用 `up --no-build --no-deps -d --wait web` 更新正式 Web。镜像与实际生效的覆盖文件路径见本次部署记录。构建受限时可复用已验证 Node/nginx 基础镜像，仍对新源码执行测试、类型检查和构建。

若 Web 发布失败，恢复原镜像及覆盖文件，只重新创建 Web；API、Worker、数据库和文件卷不操作。若公网代理失败，停止或撤回新增 Caddy 配置，保留证书与日志，继续通过原 SSH 隧道访问 `3000`。云安全组回退只删除本轮明确新增的规则。

## 验证范围

先用隔离数据库和模拟模型进行浏览器回归，验证登录前后备案号、桌面/手机布局、权限、历史和文件下载，以及轮询进度、页面恢复和主动停止。公网验证域名、可信证书、HTTP 跳转、页面与静态资源、未登录受保护接口和账号自身认证。真实生产账号流程与真实问答按部署记录分别列出已验证和待人工验收项；本轮代理发起的付费模型请求数为 0。

官方依据：[自动 HTTPS](https://caddyserver.com/docs/automatic-https)、[系统服务](https://caddyserver.com/docs/running)、[流式代理](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#streaming)。
