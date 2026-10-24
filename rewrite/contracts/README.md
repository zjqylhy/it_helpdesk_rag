# 机器契约（M2）

由新领域模型和 FastAPI 路由生成，未引用旧业务实现：

```powershell
rewrite/backend/.venv/Scripts/python.exe -m helpdesk.entrypoints.contracts rewrite/contracts
```

- `openapi.json`：当前 HTTP 接口。POST 问答首次受理返回 202 JSON，重复请求返回 200 原消息；GET 会话消息读取进度或结果，独立 stop 接口停止后台任务。模型未配置仍返回 503。
- `coverage-v1.schema.json`、`answer-check-v1.schema.json`：内部结构与基础一致性校验；授权 ID、原文区间、上下文 hash、任务全集及语义检查仍需 M4/M5 实现。
- `chat-request-v1.schema.json`：浏览器允许提交的字段，不能指定可信主体和策略。
- `chat-receipt-v1.schema.json`：受理和停止的 JSON 回执，包含消息编号、原请求编号及当前状态；不是最终答案。
- `public-event-envelope-v1.schema.json`：图内执行和评测的事件包络，不作为浏览器传输协议；浏览器轮询消息表投影，内部 trace 不返回前端。
- `prompt-manifest.json`：当前 `push-domain-v1` 推送提示词的字面量哈希；`prompt-manifest-legacy-it.json` 单独保留旧 IT 提示词的来源与哈希。当前协议使用 `push-wire-*` 样本核对，历史 `scope-wire-*` 样本不改写为推送效果。

JSON Schema 不能表达全部跨对象关系；运行时模型校验和后续证据校验仍为必需。生成文件只描述接口，不证明自适应检索或检查器有效。
