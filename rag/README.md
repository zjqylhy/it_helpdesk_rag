# 历史离线评测

此目录保留已冻结的旧评测协议、数据集维护、检索重放、Ragas 适配与报告生成。在线 API 和 Worker 已统一到 `rewrite/`，本目录不再提供 HTTP 服务或后台入库入口。

使用独立虚拟环境，避免同名 `helpdesk` 包与在线应用混用：

```powershell
uv sync --locked --project rag --extra test --extra faithfulness
uv run --project rag python -m helpdesk.evaluation.cli --help
uv run --project rag python -m pytest rag/tests -q
```

历史配置继续读取根目录 `.env`；模板在 `rag/.env.example`，不要用它覆盖在线 `.env.python`。数据集和报告保存在忽略的 `eval/private/`、`reports/`。涉及数据库导出或真实模型的命令必须显式使用正确的旧库/副本与调用预算。

保留的 `pipeline`、`store`、`providers`、`scope`、`access` 和 `parsing` 被离线评测实际使用；它们不被新应用导入。旧 HTTP 集成脚本已删除；线上业务、权限、SSE 与 Worker 回归由新应用测试负责。

历史评测操作说明见 [评测文档](../docs/evaluation.md)，已有结果见 [原验证记录](../docs/verification.md)。新实验用 `helpdesk-eval`；不能把旧分数当作新实现验收结果。
