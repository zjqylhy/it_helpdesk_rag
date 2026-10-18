# Python 在线应用

本目录是项目唯一在线实现：FastAPI API、LangGraph 基线问答、独立 Worker、统一 PostgreSQL 数据访问与 Vue 前端。根目录已移除旧 Go API 和旧前端。

配置、启动和验证统一见 [README](../README.md)、[Windows 开发](../docs/windows.md)、[部署与数据恢复](../docs/cloud.md)。API 启动不建表、不重置密码、不启动 Worker。推理关闭或未配置模型时问答返回 503。

需求与技术方案见 [设计入口](../docs/redesign/README.md)，本次源码迁移与验证见 [Python 统一迁移](../docs/python-unification.md)。历史离线评测仍使用独立的 `rag/` 环境。M4/M5 覆盖补查和回答检查尚未完成，迁移不改变此前的策略裁决。
