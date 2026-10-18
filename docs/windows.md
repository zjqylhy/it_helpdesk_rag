# Windows Python 开发

需要 Python 3.12、uv、Node.js 和带 pgvector 的 PostgreSQL。数据库可使用独立容器或已有的开发实例；新应用不依赖 Go。

1. 根目录执行 `tools/windows.ps1 -Action Init`，生成 `.env.python` 和随机数据库密码；已有文件会拒绝覆盖。历史 `.env` 保留。
2. 执行 `tools/windows.ps1 -Action Setup`，按锁文件安装 Python 与新前端依赖。
3. 编辑 `.env.python` 的 `HD_DATABASE_URL` 和 `HD_UPLOAD_DIR`。数据库需允许安装 vector 扩展；上传根目录使用绝对路径，API 可写、Worker 读取相同目录。
4. API 停止时执行 `tools/windows.ps1 -Action Migrate`，显式初始化或升级 schema。
5. 空账号库执行 `tools/windows.ps1 -Action Bootstrap -Username admin`，交互输入密码。已有账号库不会重置密码。
6. 三个终端分别执行 `tools/windows.ps1 -Action Run -Service Api`、`-Service Worker`、`-Service Web`。

网页为 [http://127.0.0.1:13000](http://127.0.0.1:13000)，API 默认 18080。`Check` 只读检查配置和数据库，不调用模型。脚本从任意目录调用时都会切到项目根，统一文件根与配置路径。

模型配置为 `HD_CHAT_*`、`HD_EMBEDDING_*`、`HD_RERANK_*`；URL 填完整端点，模型名记录真实身份，网关别名单独填写 `*_REQUEST_MODEL`。默认推理关闭；配置完整后设 `HD_MODEL_INFERENCE_ENABLED=true` 并重启 API 与 Worker。网关 CA 采用本机绝对路径；不要将容器的 `/run/certs/...` 路径用于原生服务。

## 验证

设置独立测试管理连接 `HD_TEST_DATABASE_URL` 后执行 `tools/windows.ps1 -Action Verify -Browser`。测试每例创建、销毁随机命名的合成数据库，不使用 `HD_DATABASE_URL`。本仓库已有原生数据库运行包时，也可先执行 `rewrite/backend/.venv/Scripts/python.exe rewrite/scripts/test_postgres.py start`，在同一终端会话完成验证后执行 `stop`。

PowerShell 脚本使用 UTF-8 BOM，兼容 Windows PowerShell 5.1。Docker 方式见 [README](../README.md)。历史原生数据库维护工具 `tools/postgres.py` 仅用于旧资料和离线评测，不作为新应用初始化入口。
