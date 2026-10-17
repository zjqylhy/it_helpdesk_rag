# M3 限流与观测修复交付记录

日期：2026-10-17。需求及 [修复方案](m3-reliability-spec.md) 已由用户确认并授权编码、服务器零模型调用验证。代码 `cb413c98c50c3dd1cf254752478eab0de2ac7193` 已部署到服务器隔离候选环境，自动验证与部署核对完成；用户已回复“验收，继续准备重跑方案与预算”，完成本批代码/测试验收。此轮不包含真实重跑或新的效果结论。

## 已实现

1. **跨题共享调度**：评测一次创建 RateGovernor，baseline/split-v1 共同遵守按上游配额键分组的 60 秒窗口、最小间隔和估算单位限制。独立 `scheduling.sqlite` 保存准入和冷却；不退款扩大窗口，重启不清状态。路由等待逐段复核权限和 deadline，等待本身不增加 HTTP 账本槽。
2. **429 有界处理**：适配器独立识别 429，解析非负整秒或 HTTP-date；缺失/非法时使用 60 秒冷却。合法长等待不会截短后提前重试。非流式仍至多重试一次，流式不自动重放；所有真实重试重新经过原请求/实验预算。错误诊断只保留状态码、规范化等待及受限 request-id。
3. **部分阶段诊断**：原 LangGraph 节点增加私有 observer，按 updates 保存已完成的候选/融合/重排/上下文 ID 与哈希。即使 sufficiency 或生成失败，前序覆盖仍能计算；observed、not_reached、failed、unknown 分开，未知覆盖为 null。未完成草稿和证据全文不新增到诊断。
4. **用量和费用**：输入/输出独立汇总已知小计；Embedding/Rerank 标识输出不适用及字段来源，原始输出缺失仍可辨别。仅输入模型允许用 total_tokens 作为输入；Chat 不猜分摊。无效答案响应里已知的用量也保留。费用根据显式冻结价目推算，未知请求不填零，实际账单始终单列。
5. **新版冻结协议**：`rewrite-eval-v2` 绑定调度配置、重试/轨迹/用量版本及可选价目哈希。真实 execute 没有 rate-profile 即拒绝。启动标记绑定 manifest；已经执行过而账本/调度状态缺失时停止，不能自动重建状态恢复预算。旧 v1 目录和报告仍使用原工具，不用新代码续接或覆盖。
6. **对外边界**：保留现有公开 SSE 摘要字段，逐调用诊断和节点状态仅写入受限 RunJournal/评测结果。没有修改模型提示、拆解条件、检索/融合/重排、上下文选择或引用规则；没有业务数据库迁移，没有启用 M4/M5。

客户端调度只协调当前评测进程，不能保证共享网关永不再限流。估算窗口单位不等于提供商 Token，网关继续是跨应用限额的权威。新的配置不自动应用到候选 API/Worker，更不修改共享网关额度。

## 配置及操作

freeze 新增 `--rate-profile /path/rate-profile.json` 和可选 `--prices /path/prices.json`。前者需要 schema `rewrite-rate-profile-v1`、gateway_version、端点身份哈希、估算算法及三个路由。每路由明确 request_model、provider_id、model_id、upstream、quota_key、rpm、units_per_minute、candidate_count=1、gray_percent=0；同上游不能拆配额规避限制。配置内容与 Settings 的实际请求别名、声明模型和端点必须一致。

价目需有版本、CNY 币种、三类输入价和 Chat 输出价。它影响报告估算，不提供人民币金额熔断。本次只验证协议；真正重跑前另行核对实时路由、配额和价目并申请预算。

```sh
python -m helpdesk.entrypoints.evaluate freeze /input/all.jsonl --out /artifacts/NEW_DIRECTORY --split dev --strategies baseline,split-v1 --variants D --seed 12 --max-calls 150 --reservation-limit 1600000 --source-corpus /input/source-corpus.json --rebind-approval /input/approval.json --rate-profile /input/rate-profile.json --prices /input/prices.json
python -m helpdesk.entrypoints.evaluate run /artifacts/NEW_DIRECTORY
```

以上 run 没有 `--execute`，只校验，不产生模型请求。限额参数仅为协议示例，不构成新的费用授权。旧批次剩余额度不转入新实验。

## 服务器验证

| 项目 | 结果 |
| --- | --- |
| 最终完整 Python 回归 | **142 passed，175.71 秒** |
| 静态检查 | Ruff 通过；mypy **72 个源码文件**通过 |
| 调度/重试故障注入 | 共享窗口、独立路由、最小间隔、冷却恢复、时钟回拨、状态损坏、取消及权限变更；等待不足不占第二个 HTTP 槽 |
| 生命周期与预算 | 429/5xx、流式不重放、输入模型字段来源；预算不足不等待；状态丢失不重建；调度配置漂移拒绝 |
| 失败轨迹 | scope、split、embedding、rerank、sufficiency、generate 六处注入失败；已完成覆盖保留，未执行为 null，实际空候选为 0 |
| 公开/私有分离 | 真实合成 API/SSE 回归中，错误终态的私有诊断保留；公开响应不含 attempt_details、provider_request_id、node_observations |
| 旧/新合成协议保真 | 隔离无网络容器分别运行已验收 e2450bc 与新代码，模型载荷、公开事件、正文和引用完全一致；两策略合成调用数为 5/8 |
| 发布独立性 | 无网络离线 wheel/sdist 构建通过；**71 个模块**可从 wheel 隔离导入，6 份冻结提示可读 |

合成协议指纹均为 `7379ec2f39d85f387a06b8e8c3c2b0479d4d33695d6246bcc0268c88b2bfcefb`。该检查证明相同合成回包下业务输入/输出未改变，不证明真实质量提升或真实网关限流已消除。

此前曾对工作快照运行针对性测试（53 passed）；该结果由上述最终完整回归替代。中途被最终快照替代的旧回归经 SIGINT 停止，不记为最终通过结果。全部业务验证在服务器完成，本地仅编辑、归档传输和 Git 操作；测试容器没有真实模型凭据，HTTP 故障由 MockTransport/FakeModels 产生。

## 候选部署与交付证据

服务器证据目录：`/opt/it-helpdesk-rag-rewrite-m3/artifacts/reliability-20261017/`。API、Worker 和 Web 均运行代码版本 `cb413c9`；API/Worker 的 `HD_MODEL_INFERENCE_ENABLED=false`。候选健康检查通过且 `chat_ready=false`，原生产四个服务健康，未切换正式入口。

| 核对项 | 结果 |
| --- | --- |
| API/Worker 镜像 | `sha256:615286af8d7c5aa707d92308bbdfe831b5dba3059f4242bcc4ce4d8b8191578a` |
| Web 镜像 | `sha256:6bc7ef2f3e5c714d4df7ec4e77692d206758179df232f9dfc6fe677ad33e517a` |
| 源码对应关系 | 最终受测的 94 份源码/测试/验证文件与提交归档一致；运行镜像的 77 份源码/迁移文件与部署清单一致 |
| 新协议零调用核对 | 使用原已批准来源冻结 dev 10 题、20 条计划；`rewrite-eval-v2` 校验通过，模型请求 0，推理关闭，未创建执行标记或调度数据库 |
| 原实验保护 | 仍为 59 次请求；原交付清单中的文件哈希全部一致，答案及人工待审状态未改动 |
| 候选数据库 | 授权版本 28，API 模型尝试数 0，已完成入库任务 11 |
| 本轮新增真实推理 | **0 次** |

零调用冻结目录仅用于协议验证，标记 `paid_execution_authorized=false`，不得用它直接启动下一轮真实实验。调度配置哈希为 `3fea036a7bb42e5c07363272794e71cbbe1a792a179392fda46786f2b48cd54f`；来源仍绑定原已批准包，配置和旧价目只用于本轮冻结验证，不构成新预算。

本地忽略目录中的证据副本：[部署核对](../../reports/rewrite-m3-reliability/reliability-20261017/delivery-validation.json)、[最终测试日志](../../reports/rewrite-m3-reliability/reliability-20261017/final-pytest.log)、[证据清单](../../reports/rewrite-m3-reliability/reliability-20261017/delivery-manifest.json)。证据包含私有题目及语料，仅保存在受限服务器与本地忽略目录，不提交 Git。

证据归档 `reliability-evidence-cb413c9.tar.gz` 的 SHA256 为 `08e6015a18de1270986cdde0ec4d3f249bded36d0ccb5c247bdc7b7d960f9537`；归档内交付清单 SHA256 为 `f927384bf212027ef1079a94a1a581bb6c123576ecdd78af898fbb0e532120db`。

## 后续门禁

本批代码/测试已人工验收；[新 dev 重跑方案与预算](m3-rerun-budget.md) 已准备并完成服务器零调用核对，待新的费用及载荷发送批准。固定两个策略、题目与统一调度配置，说明等待对总超时/P95 的影响，重核模型路由与价格。原 13 条失败保留，不能选择性删掉或用本轮替身结果填补。
