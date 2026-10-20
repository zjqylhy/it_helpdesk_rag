# 2026-10-18 Python 主入口部署

用户明确要求提交 PR、部署到服务器，并确认“迁移现有数据并切换主入口 3000”。源码与运行镜像版本为 `179ac075d884e4b49b648dd0f92440dc9c1b9411`，PR 为 [#2](https://github.com/zjqylhy/it_helpdesk_rag/pull/2)。本记录是后续部署批次，不改写之前源码迁移时的验收结果。

## 实际发布

- 服务器：`8.155.133.72`；目录 `/opt/it-helpdesk-rag-python-20261018/source`，Compose 项目 `it-helpdesk-python`。
- 北京时间 2026-10-18 23:32 完成主入口切换：服务器回环 `3000` 为新 Python/Vue，API 与 Web healthy，Worker 正常运行，数据库 healthy。新数据库回环 `55534`，使用独立数据库与文件卷。
- 旧 `it-helpdesk-rag-cloud` 的 Web、Go API 和 RAG 已停止，旧数据库及文件卷保留。`3001` 的 M3 候选环境保持原状。
- 模型配置和 CA 在服务器内部从既有候选环境复制；没有重新发送模型密钥到服务器或 GitHub。新 API/Worker 推理已启用，`chat_ready=true`，经原 CA、逻辑 Host/SNI 与服务器隧道的 `GET /v1/models` 为 200。此次部署没有发送付费推理请求或重跑质量实验。
- 本地 SSH 访问为 [http://127.0.0.1:13000](http://127.0.0.1:13000)，映射服务器回环 3000。沿用原账号密码，恢复库中的旧登录令牌已清除，需要重新登录；验证会话已退出。没有开放新的公网 HTTP 端口。

## 数据与验证

旧写入者停止后，在同一维护窗口备份数据库及上传卷，恢复到全新目标库，显式执行三次版本迁移与原文件映射。迁移和文件映射前后，16 张旧表的原字段行数及完整数据指纹完全一致；新字段单独处理，未重建向量或生成历史答案。

| 数据 | 保留数量 |
| --- | ---: |
| 知识库 | 9 |
| 文档 / 版本 / 原文件 | 11 / 11 / 11 |
| 向量切片 | 723 |
| 会话 / 消息 | 20 / 30 |
| 用户 / 组 / 组成员 | 3 / 1 / 1 |
| 文档组授权 / 审计事件 | 3 / 17 |

11 份文件从明确的旧版本路径建立相对映射，复制到新文件卷后校验哈希，且全部通过新 API 下载后再次校验。管理员实际读取 18 个会话和 27 条消息；另外用户的会话与消息在数据库指纹核对中保留。

服务器验证：Ruff、72 个源码文件 mypy、146 项真实 PostgreSQL 集成/单元测试、13 组冻结协议（无差异）、14 项浏览器流程（页面错误 0）通过。测试采用独立合成数据库和 HTTP 模型替身，不使用生产库或真实模型。发布镜像中的 79 份运行源码、锁文件及迁移文件与提交源码逐项 SHA256 一致。新前端在服务器通过 9 项 SSE 测试及 TypeScript/Vite 构建。本地 SSH 入口实际核对健康响应及新前端资产。

[PR CI](https://github.com/zjqylhy/it_helpdesk_rag/actions/runs/39139942466) 的 build-test 和 compose-smoke 均成功；它验证 GitHub 的标准 BuildKit 路径，服务器的受限构建方式另见下节。

## 构建与证据

源码包包含 187 个文件，基准提交为上述完整 revision、工作区干净；包 SHA256 为 `a9815d109469711fcb038520cd9ce1706e71e29d90a77695e2ef10d6a15dd202`。

服务器无法使用标准 Docker Hub 基础镜像，复用已验证的 Python 3.12、Node 24、nginx 缓存，保持 `uv.lock` 与原服务器完全一致。Python 运行镜像使用原 Dockerfile 的 runtime 阶段，构建依赖通过可信镜像源取得，业务依赖版本和哈希未变。旧构建器会顺序执行无关的 verify 阶段，因此 Web 发布只使用已成功测试/编译的新 frontend 阶段及同一 nginx 发布指令。最后显式覆盖缓存基镜像继承的旧 revision 环境变量和标签。

运行镜像：

- API / Worker：`helpdesk-python:179ac07`，`sha256:1845441eed05dcba6287e8dc20dfabc2525ff15b72508b0759927913a10af8ad`。
- Web：`helpdesk-python-web:179ac07`，`sha256:e71bbcb741fc245051dfd733dcce1fe62c45699d88409e8adca277c307141400`。

服务器受限目录保留源码清单、镜像清单、QA 日志、数据指纹和部署结果：`/opt/it-helpdesk-rag-python-20261018/artifacts/`。完整备份在同目录的 `backup-20261018/`，文件权限 600：

- `rag.dump`：3,631,572 字节，SHA256 `86ce703a04f0c4d7b978049e118dd1eb19ce2fa7cd9c2100f7e001217962bd2f`。
- `uploads.tar.gz`：25,737 字节，SHA256 `b060523b8d978f42b011c12e5b01db2a3feff7422a2ffbbd7515879df2e6b5f0`。

部署配置、备份、登录信息与业务内容不进入 PR。

## 维护和回滚

新项目命令固定使用：

2026-10-21 公网 HTTPS 发布后，维护命令增加 Web 镜像覆盖文件，详见 [公网发布记录](public-https-deployment-20261021.md)。

```sh
cd /opt/it-helpdesk-rag-python-20261018/source
docker compose --env-file .env.python -p it-helpdesk-python -f compose.yaml -f compose.server.yaml -f compose.public-https.yaml ps
```

旧项目的原配置为 `/opt/it-helpdesk-rag/compose.yaml` 与 `compose.release.yaml`。切换脚本在验证失败时会停止新写入者并恢复原项目；本次没有触发回滚。现在新入口已经上线，后续若需回滚，应先停止新写入并备份新库/文件，核对上线后的增量；不能直接启动停留在切换前快照的旧库而丢弃新数据。完整迁移与恢复原则见 [部署说明](cloud.md)。

这次完成生产源码发布、数据迁移和入口切换；没有合并 PR、执行正式质量评测或新增覆盖补查/回答检查。
