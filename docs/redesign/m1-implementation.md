# M1 基础实现与验收

日期：2026-10-17。需求与全 Python 技术方案已由用户确认。本阶段代码/测试已由用户回复“确认”验收。以下保持 M1 交付时快照；后续进度见 [M2](m2-implementation.md)。

## 交付范围

新代码位于 [rewrite](../../rewrite/README.md)，不导入旧 Go、Pipeline、Store 或 Vue 组件。原应用入口与 Compose 尚未切换，业务数据库、原文件、私有题集和历史成绩未修改。

| 能力 | M1 已实现 | 后续部分 |
| --- | --- | --- |
| 业务分层 | domain、application、ports、HTTP/PostgreSQL/FileStore 适配；请求依赖在 lifespan 装配 | M2 接入同进程 LangGraph 与真实模型适配 |
| 身份 | bcrypt、哈希 Token、登录/注销、账号停用、改密撤销 Token、可信账号来源、显式空库管理员初始化 | 完整账号管理页面在 M2 |
| 权限 | 用户组、组成员、文档全员/组授权、空组仅管理员；策略/revision/审计同事务 | M2 将相同约束接到检索、模型输入与 SSE 逐事件输出 |
| 撤权 | 异步读写屏障、旧运行取消注册表、普通 HTTP 响应发送前复核；数据库会话锁拒绝第二 API 进程 | 尚无问答流；不能宣称已完成 SSE 在途撤权联测或分布式协调 |
| 文档 | 知识库、默认范围配置、20 MiB 上传、哈希重复确认、新版本/任务入队、列表/预览/下载、启停、失败任务重试 | Worker 解析、切片、Embedding、原子发布与租约恢复在 M2 |
| 文件 | 卷内 storage_key、完整性校验、失败补偿、取消清理、提交确认未知时保护已被引用文件；显式历史文件映射 | 提交不确定且数据库不可用时保守保留文件，切换前需离线核对孤儿；不以强删解决 |
| 会话 | 归属、all/selected 范围、历史；失权时遮蔽回答、引用、trace 与派生标题；数据库准入幂等/生成互斥/终态不可覆盖 | 准入与终态目前为应用接口，M2 才接在线问答和重启清理 |
| 机器契约 | OpenAPI、覆盖/回答检查/问答请求/SSE 包络 JSON Schema、ChatRunner 类型端口 | Schema 与端口不代表检查器、预算器、图或 SSE 已实现 |
| 前端 | 新 Vue 登录、恢复与注销、授权知识库/会话列表、失败清除旧数据、桌面/移动骨架 | 问答、知识管理与权限编辑的完整交互在 M2 |
| 自适应与评测 | 本阶段没有新增策略或模型调用 | M3 先闭环 baseline/split-v1；M4 一次补查；M5 独立检查/一次修正与收益评测 |

`/api/v1/chat` 在 M1 明确返回 503，页面明确问答尚未开放。不生成假答案、不把替身或状态包络当作已上线流程。

## 外部契约与迁移

旧程序仅在一次性 synthetic 数据库运行，作为外部 HTTP 行为对照；采集工具见 [capture_legacy_contracts.py](../../rewrite/scripts/capture_legacy_contracts.py)，不会被新应用调用。冻结的合成响应和旧表结构放在 [fixtures](../../rewrite/backend/tests/fixtures/legacy-api.json)，不含真实业务内容或 Token。

适配器保留 `/auth/me` 的 `ID/Role`、创建资源的 201、上传的 202、管理写操作的 204，以及失败任务重试的空 202。默认问答设置使用旧 `enabled` 字段；账号更新兼容 `PATCH /admin/users/{id}`，另提供明确的 `/disabled` 操作。新账号 ID 仍为不透明的 32 位字符串。错误保留可读的 `error` 字段，新增稳定 `code`，具体提示文案不作为冻结的逐字契约。

迁移使用独立 `app_migrations` 校验和账本与事务锁，不覆盖旧 `schema_migrations`。API 运行时拒绝迁移；API 启动只校验版本，不执行 DDL、不重置账号密码。SQL 使用 LF，清单绑定实际字节。

历史文件保持旧 `path`，新增可空 `storage_key`。未映射的历史文件下载返回明确错误；映射工具只接受显式“版本 ID→来源根内相对路径”，逐文件比较数据库哈希后复制到新根，不删除或改写旧原文件。生产文件不能凭旧绝对路径推测映射。

## 复现方式

从仓库根目录安装新工程依赖：

```powershell
uv sync --locked --project rewrite/backend
npm --prefix rewrite/frontend ci
```

API 与 CLI 只读取 `HD_` 配置，不自动读取根目录旧 `.env`。为独立数据库配置 `HD_DATABASE_URL`，为新文件根配置 `HD_UPLOAD_DIR` 后执行：

```powershell
rewrite/backend/.venv/Scripts/python.exe -m helpdesk.entrypoints.migrate --directory rewrite/db/migrations
rewrite/backend/.venv/Scripts/python.exe -m helpdesk.entrypoints.admin admin
rewrite/backend/.venv/Scripts/python.exe -m helpdesk.entrypoints.api
```

管理员命令交互输入密码，仅限空账号库执行；密码为 12–72 字节。API 默认仅监听 `127.0.0.1:18080`。另一个终端运行 `npm --prefix rewrite/frontend run dev`，页面默认 `127.0.0.1:13000`。前端代理可通过 `HD_API_TARGET` 指定隔离 API，不把数据库或模型凭据交给浏览器。

本机已有原生 PostgreSQL 运行包时，可运行以下命令启动**独立测试实例**；新集群在忽略的 `data/rewrite-m1-postgres/`，动态选择本地可用端口，不读取业务数据库凭据：

```powershell
rewrite/backend/.venv/Scripts/python.exe rewrite/scripts/test_postgres.py start
rewrite/scripts/check.ps1 -Browser
rewrite/backend/.venv/Scripts/python.exe rewrite/scripts/test_postgres.py stop
```

其他环境可设置 `HD_TEST_DATABASE_URL` 指向隔离的 PostgreSQL/pgvector 测试实例。测试账号需要创建/删除测试数据库权限；每例只操作随机 `m1test_` 数据库。浏览器验证脚本目前使用本机测试实例状态；Linux 可指定 `HD_BROWSER`，或安装 Playwright Chromium。命令与工具不得指向生产数据库。

Windows API 显式创建 Selector 事件循环，避免 Uvicorn 默认循环与 psycopg 不兼容；不依赖全局事件循环策略碰巧生效。

## 验证结果

| 项目 | 本轮结果 |
| --- | --- |
| 后端 | 39 项 pytest 通过；真实 PostgreSQL/pgvector，无内存数据库代替事务/锁验证 |
| 静态检查 | Ruff 通过；mypy 检查 36 个源文件通过；依赖锁一致 |
| 迁移 | 空库初始化、重入不重复、旧 v2 schema 合成副本升级、数据/向量/历史不变、SQL 失败回滚、已执行文件篡改拒绝 |
| 权限与故障 | 私有元数据与文件隐藏、组并集/撤权、历史与标题遮蔽、响应发送竞态、取消、失效 Token、最后管理员、审计无密码 |
| 文件与会话 | 重复确认/清理、非法格式/空文件/超限、完整性与路径边界、映射批次回滚、提交确认未知不误删、并发幂等与终态保护 |
| 前端 | vue-tsc 与 Vite 构建通过 |
| 浏览器 | 真实 API+Vue 的 7 项检查通过；1440×1000 和 390×844，页面错误0；已查看桌面管理员与移动成员截图 |
| 打包独立性 | wheel/sdist 构建通过；隔离模式从 wheel 加载 28 个模块并生成 OpenAPI，未加载旧源码 |
| 模型与线上 | 付费模型调用0；未推送、部署或切换现有应用 |

本轮 Docker daemon 不可用，因此使用独立原生数据库；没有宣称 Linux/Compose 已验收。旧 schema 升级验证使用冻结表结构和合成数据副本，尚未对真实业务数据执行升级/备份恢复；完整副本演练与统一切换仍属于 M6。

测试输出、浏览器截图、临时日志和打包产物保存在忽略的 `data/rewrite-m1-*`，不把本地合成验证计为正式效果成绩。按用户提供的 AGENTS.md，“代码编写与测试”节点需人工确认；本批验收后再进入 M2。
