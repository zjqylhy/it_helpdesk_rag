# Python 部署

统一使用仓库根目录 `compose.yaml`、`.env.python` 和 `tools/docker.ps1`。原预览 Compose 与重复配置模板已移除，避免出现两套启动入口。

当前 Dockerfile 保留 runtime、verify、browser、web target，依赖与模型隔离策略不变。完整命令、私有 CA、旧数据副本迁移、备份与恢复见 [部署说明](../../docs/cloud.md)。服务器已存在的旧预览环境按原固定提交维护，升级时显式切到新配置，不能仅替换文件后复用不明卷。
