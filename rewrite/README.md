# 全 Python 独立重实现

已确认规格：[设计入口](../docs/redesign/README.md)。本目录不导入根目录旧业务代码，不连接旧业务库。M1 为身份、权限、文档元数据/上传、会话和契约基础；解析发布、真实问答、LangGraph 执行与完整管理页面属于 M2 以后。

API、迁移和显式账号初始化使用独立命令。启动 API 不建表、不改密码、不启动 Worker。M1 `/api/v1/chat` 明确返回 503，不输出模拟答案或伪造 SSE 成功。

运行与验收说明见 [M1 实施记录](../docs/redesign/m1-implementation.md)。
