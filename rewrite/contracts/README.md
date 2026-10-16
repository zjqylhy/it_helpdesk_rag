# M1 机器契约

由新领域模型和 FastAPI 路由生成，未引用旧业务实现：

```powershell
rewrite/backend/.venv/Scripts/python.exe -m helpdesk.entrypoints.contracts rewrite/contracts
```

- `openapi.json`：M1 HTTP 接口。问答是明确的 503 占位，不代表图已运行。
- `coverage-v1.schema.json`、`answer-check-v1.schema.json`：内部结构与基础一致性校验；授权 ID、原文区间、上下文 hash、任务全集及语义检查仍需 M4/M5 实现。
- `chat-request-v1.schema.json`：浏览器允许提交的字段，不能指定可信主体和策略。
- `public-event-envelope-v1.schema.json`：SSE 业务事件包络；各事件 data 的公开白名单及编码由 M2 实现，不能把任意图状态塞入 data。

JSON Schema 不能表达全部跨对象关系；运行时模型校验和后续证据校验仍为必需。生成文件只描述接口，不证明自适应检索或检查器有效。
