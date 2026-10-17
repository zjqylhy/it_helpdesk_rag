# 全 Python 独立重实现

已确认规格：[设计入口](../docs/redesign/README.md)。本目录不导入根目录旧业务代码。M1 基础已验收，M2 已接入独立 Worker、LangGraph 基线、模型网关、评测入口和完整业务页面，当前等待本批代码/测试验收。没有切换原应用或业务数据库。

API、Worker、迁移、账号初始化和评测使用独立命令。启动 API 不建表、不改密码、不启动 Worker。未配置模型时问答返回 503；正式入口不会启用测试替身。

当前运行方式与限制见 [M2 实施记录](../docs/redesign/m2-implementation.md)，此前基础记录见 [M1](../docs/redesign/m1-implementation.md)。
