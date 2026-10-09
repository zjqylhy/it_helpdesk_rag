# Windows 原生运行

本项目不要求安装 Docker Desktop。前端、Go API 和 Python RAG 都可在 Windows 上直接运行，数据库使用原生 PostgreSQL + pgvector，可以位于本机或远程服务器。

## 1. 配置与依赖

在项目根目录执行下面两步。已有 `.env` 时跳过 Init，该命令拒绝覆盖现有配置；Setup 安装项目依赖，需要联网。

```powershell
.\tools\windows.ps1 -Action Init
.\tools\windows.ps1 -Action Setup
```

## 2. 项目独立数据库

```powershell
.\rag\.venv\Scripts\python.exe .\tools\postgres.py install
.\rag\.venv\Scripts\python.exe .\tools\postgres.py init --port 55433
.\rag\.venv\Scripts\python.exe .\tools\postgres.py status
```

install 使用 SHA256 校验的 Micromamba 和 conda-forge Windows 包，固定 PostgreSQL 16.15 与 pgvector 0.8.6，无需本地编译扩展。参见 [Micromamba 官方说明](https://mamba.readthedocs.io/en/stable/installation/micromamba-installation.html) 和 [conda-forge pgvector](https://anaconda.org/conda-forge/pgvector)。运行包安装到项目目录，不注册 Windows 服务，不修改系统 PATH。

init 创建随机数据库密码、非超级用户 rag、业务库 rag，并启用 vector。默认只监听 `127.0.0.1:55433`，避开本机已有 5432 服务。重复 init 保留数据、账号与密码，初始化中断后的建库步骤可续做。

示例 DATABASE_URL 会替换为项目独立数据库连接，其他配置保留。自定义 DATABASE_URL 不会覆盖。运行包、数据、日志和凭据均位于 Git 忽略的 `data/native-postgres/`；`credentials.json` 含本地管理凭据，请勿分享或提交。备份时先正常停止数据库，再备份该目录及上传文件。

```powershell
# 重启电脑后先启动数据库；不自动开机启动。
.\rag\.venv\Scripts\python.exe .\tools\postgres.py start
# 正常停止，保留数据。
.\rag\.venv\Scripts\python.exe .\tools\postgres.py stop
```

也支持自行安装或远程 PostgreSQL，参见 [PostgreSQL Windows 下载页](https://www.postgresql.org/download/windows/) 和 [pgvector 官方说明](https://github.com/pgvector/pgvector#windows)。在目标库启用 vector，将业务连接写入 `.env`，不需要执行本项目的 install/init。

## 3. 模型配置与检查

在项目根目录的 PowerShell 中执行：

```powershell
# 编辑 .env，填写两家模型 Key 和已确认的阿里业务空间 HTTPS 主机根地址。
# 已有配置若仍有 replace-with-，替换内部令牌与两个本地账号密码。
.\tools\windows.ps1 -Action Check
```

Init 创建配置时会生成随机本地账号密码和内部令牌。已有配置需确认 INTERNAL_TOKEN 至少 24 字符，账号密码 12–72 字节。Check 只读取配置、探测数据库和 pgvector，不调用模型，也不打印密码或 Key。返回 0 表示检查通过，返回 1 会提示缺少的配置。

模型 Key 也可以配置在 Windows 用户或系统环境变量中，名称为 `DEEPSEEK_API_KEY` 和 `DASHSCOPE_API_KEY`（兼容 `QWEN_API_KEY`）。Windows 脚本会补入当前进程尚未继承的这些变量，不写回 `.env`；已存在的进程变量优先。直接从终端启动服务时，应使用能读取这些变量的新终端。

需要真实协议联调时运行 `tools/windows.ps1 -Action ModelCheck`：检查 Embedding 文档/查询输入、重排、JSON 生成及流式终态，最多发出五次模型 HTTP 请求（重试也计入），使用虚构短文本，不打印凭据、向量和回答原文。该操作会产生提供商调用费用；日常配置诊断仍使用不调用模型的 Check。

如果 PowerShell 提示执行策略限制，可以对这次调用使用：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\windows.ps1 -Action Check
```

该参数不修改系统的永久执行策略。

## 4. 启动

分别打开三个 PowerShell 终端，切换到项目根目录，按顺序执行：

```powershell
# 终端 1：业务服务，先确认监听成功。
.\tools\windows.ps1 -Action Run -Service Backend

# 终端 2：RAG 服务与导入 Worker。
.\tools\windows.ps1 -Action Run -Service Rag

# 终端 3：前端。
.\tools\windows.ps1 -Action Run -Service Web
```

打开 [http://localhost:5173](http://localhost:5173)，使用 `.env` 中的 ADMIN_USER / ADMIN_PASSWORD 登录。每个终端用 Ctrl+C 停止对应应用，数据库另用 stop 管理。文件保存在配置的 UPLOAD_DIR；应用停止后数据库与原文件仍保留。

本机已完成 PostgreSQL 16.15 + pgvector 0.8.6 初始化和跨服务集成验证，业务库未写入测试内容。工程测试使用独立数据库和本地模型替身；真实模型及质量评测仍需模型配置、导出素材和经审核的评测集。
