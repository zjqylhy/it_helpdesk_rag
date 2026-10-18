# Python 统一迁移

日期：2026-10-18。用户已确认本次需求与技术方案；实现完成后提交代码和验证结果供人工验收。

## 范围

- 使用 `rewrite/backend`（FastAPI、LangGraph、独立 Worker）与 `rewrite/frontend`，作为唯一在线应用。
- 删除根目录 Go API、旧 Vue、Go 构建/CI/启动依赖、旧 Go→Python 内部服务入口及仅服务旧运行链的检查脚本。
- 保留 `rag/` 中被历史评测使用的解析、模型适配、检索、数据集维护与报告代码；它是独立离线环境，不导入在线应用。保留冻结契约、迁移、原始报告与人工裁决。
- 统一根目录 Compose、Windows 工具、CI 和使用文档。保留 `rewrite/` 路径，避免改变已冻结协议和历史证据的源码位置。

## 运行与数据

新应用显式读取 `.env.python`，使用 `HD_` 配置；旧 `.env` 仅用于历史评测。新容器项目、数据库卷和文件卷独立于旧应用。迁移和管理员引导是单独命令，启动 API 不建表、不重置账号、不启动 Worker。

已有数据升级先在副本恢复，再执行 `helpdesk-migrate` 与 `helpdesk-map-files`，核对 ID、版本、向量身份、权限和文件哈希。源码迁移不执行生产入口切换或覆盖旧数据库。历史结果不重新生成。

## 验收

1. 仓库无跟踪的 `.go`、`go.mod`、`go.sum`，运行和 CI 不需要 Go。
2. Python Ruff、mypy、完整 pytest（真实隔离 PostgreSQL/pgvector）通过；旧离线评测单元回归通过。
3. Vue 协议测试、类型检查和构建通过；浏览器串联 Python API、独立 Worker、模型替身、权限和引用下载。
4. 根目录 Compose 校验、Python 镜像启动与数据库初始化/恢复验证；源码包和 wheel 不依赖旧实现。
5. 清理后提交 commit，交用户进行代码与测试验收。M4/M5 自适应能力仍按原规格推进，不因本次入口统一宣称效果提升。

验证结果在本文件末尾补充；历史验证文档保留各自原日期和适用版本。

## 本次验证结果

| 项目 | 结果 |
| --- | --- |
| Python 在线业务 | 146 项 pytest 通过；真实隔离 PostgreSQL/pgvector，覆盖旧库升级、权限、并发、SSE、Worker 和新增运行诊断 |
| 历史离线评测 | 按精简后的锁文件安装 test/faithfulness 依赖，82 项通过；旧模型适配与 Ragas 回归保留 |
| 静态检查 | Ruff 通过；mypy 检查 72 个应用源码文件通过 |
| 前端 | 9 项 SSE 协议测试、TypeScript/Vite 构建通过 |
| 协议保真 | 13 组冻结输入/模型请求/证据/终态核对，无差异；无外部模型调用 |
| 浏览器 | API、独立 Worker、Vue、本机模型替身串联，14 项检查通过、页面错误 0；复核桌面和 390px 移动截图 |
| 根目录 Compose | 独立合成项目四服务运行；API UID 10001；登录上传、容器重建后数据库与原文件保留、在线迁移拒绝均通过 |
| 恢复 | 合成数据库 pg_dump/pg_restore 到另一数据库后，账号/文档数量与迁移版本一致；测试项目及卷已清理 |
| 打包 | wheel 中 71 个模块与 6 个冻结 Prompt 独立加载；源码包 187 个文件逐项 SHA256 校验，排除旧实现、私有数据、配置与证书 |
| 工程入口 | PowerShell 语法与 UTF-8 BOM、CI YAML、根 Compose 配置检查通过；运行/CI 无 Go 依赖 |

默认 `python:3.12-slim-bookworm` / `node:22-bookworm-slim` 的 Docker Hub 令牌请求超时。本机容器构建与上述运行验证采用同一 Dockerfile，显式指定已缓存的基础镜像：

```sh
docker compose --env-file .env.python -p it-helpdesk-python build --build-arg PYTHON_BASE=python:3.12-slim --build-arg NODE_BASE=node:24-alpine api web
```

此替代仅验证 runtime/web target；verify/browser target 中复制 Node 可执行文件需要与 Debian 兼容的 Node 基础镜像，不能以 Alpine 替代。默认基础镜像的构建需在 Docker Hub 网络恢复后执行；未宣称该次失败构建通过。

本次付费模型调用 0，未执行生产数据迁移、生产入口切换、推送或远程 GitHub Actions。现有 `.env`、数据库、上传资料、私有题集和原始报告保留；本机新建 `.env.python` 供后续配置，不含模型凭据。代码与测试提交后等待人工验收。
