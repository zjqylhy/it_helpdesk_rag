# 机器契约（M2）

由新领域模型和 FastAPI 路由生成，未引用旧业务实现：

```powershell
rewrite/backend/.venv/Scripts/python.exe -m helpdesk.entrypoints.contracts rewrite/contracts
```

- `openapi.json`：当前 HTTP 接口。问答已连接 LangGraph，成功为 SSE；模型未配置仍返回 503。
- `coverage-v1.schema.json`、`answer-check-v1.schema.json`：内部结构与基础一致性校验；授权 ID、原文区间、上下文 hash、任务全集及语义检查仍需 M4/M5 实现。
- `chat-request-v1.schema.json`：浏览器允许提交的字段，不能指定可信主体和策略。
- `public-event-envelope-v1.schema.json`：进程内事件包络；HTTP 适配把 event 写入 SSE event 行，把 data 和 request_id/message_id/seq 合并写入 JSON data。公开白名单已实现，框架 updates 只在服务端读取终态，不发送浏览器。
- `prompt-manifest.json`：冻结的系统 Prompt 字面量及来源提交。新请求数据结构重新实现；系统 Prompt 相同不等于新旧完整模型输入已经通过等价验收，该核对属于 M3。

JSON Schema 不能表达全部跨对象关系；运行时模型校验和后续证据校验仍为必需。生成文件只描述接口，不证明自适应检索或检查器有效。
