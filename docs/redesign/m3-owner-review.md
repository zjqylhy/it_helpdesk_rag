# M3 r2 最终人工裁决与比较结论

日期：2026-10-17。状态：**开发集人工裁决已完成，评分分歧已关闭**。用户先声明逐条盲审完成并回复“均通过”，随后明确复核“按原标准，R0007不通过”。保留两次原话及修订关系，不修改黄金标签、原答案、运行状态或旧报告；没有使用模型补写人工结论。

## 最终裁决

| 指标 | baseline | split-v1 |
| --- | --- | --- |
| 全量计划 / 已评分 | 10 / 10 | 10 / 10 |
| 正确 / 全量正确率 | **9 / 90%** | **9 / 90%** |
| 运行失败计零 | 1：R0008 | 0 |
| 人工明确不通过 | 0 | 1：R0007 |
| 无依据陈述（可评估记录） | 0 / 9 | 0 / 10 |
| 未完成用户要求（可评估记录） | 0 / 9 | 1 / 10 |
| 可回答题中错误澄清 | 0 / 7 | 1 / 7 |
| 可回答题中明确拒答错误 | 0 / 7 | 0 / 7 |
| Recall@5（7 道共同可观测可回答题） | 1.0 | 1.0 |
| 模型 HTTP 尝试 | 37 | 47 |

全量正确率包含预期澄清、拒答的题目，不能称为 7 道可回答题的纯回答准确率。失败记录没有最终正文，其无依据/遗漏不具备可评估性，因此相关分母为 baseline 9、split-v1 10；失败仍计入全量正确率并计零，未从样本中删除。

`R0008`（baseline / 002）为 `incomplete_generation`，按已有运行规则计零，来源 `runtime_status_constraint`，不是用户提供了逐条否定理由。`R0007`（split-v1 / 002）未提供题目要求的说明，只要求再次明确题目已指明的对象；用户明确要求按原标准不通过，来源 `owner_specific_correction`。其遗漏项从未作答的冻结要求映射，并标注推导来源，不冒充用户逐条点评。

来源合计为 **18 条批量通过、1 条人工明确不通过、1 条运行失败计零**。旧 adjudicate 输出的 `human_decisions=20` 是提交记录数，不代表收到 20 条独立人工理由。

旧入口的 `false_refusal` 只计 refused 终态，两组均为 0，不能据此隐去错误澄清：split-v1 另有 **1/7（14.29%）** 的可回答题未正确作答而返回澄清。它与运行错误分开统计。未启用回答修正，原正确被改错的指标不适用。

## 对照结论与当前决策

10 对题目中 **9 对两组都正确，1 对两组都错误**；split-v1 改善 0 对、退步 0 对，成对净收益 0。两组唯一未通过的是同一道题，失败原因不同：baseline 生成不完整，split-v1 错误澄清，不能将错误类型不同算作质量提升。

本次 dev 中，两组召回及正确率均未见收益，split-v1 增加 **10 次请求，约 27%**。当前证据不支持默认启用拆解，保留 baseline 作为后续比较基准；没有切换线上策略。样本只有 10 道开发题，基线召回已达本题集上限，不推断拆解在所有复杂题目上无效。

本次完成的是开发集比较闭环。独立 test、新增困难题、M4 的覆盖/补检索、M5 的回答检查仍未执行，不自动追加调用、调整提示或提高输出上限。后续策略选择与新阶段仍单独进行。

## 服务器离线验证与不可变证据

最终修订和现有 adjudicate 入口均在服务器 **无网络、无模型凭据**容器运行：20 条唯一裁决全部通过输入校验，只有 R0007 的记录发生修订，10 对比较及两组 90% 结果复核一致。原始交付文件和前一版人工记录全部哈希一致，原账本仍为 **84 次请求**；本轮新增模型调用 **0**。未修改产品代码，也未新增本地业务测试。

新独立目录：`/opt/it-helpdesk-rag-rewrite-m3/artifacts/dev-r2-owner-review-final-20261017/`。此前的批量结论、曾有分歧的归一化摘要及原空白模板保留历史字节；不使用旧摘要的 split-v1 100% 接受率作为当前结论。

- [最终来源分离摘要](../../reports/rewrite-m3-rerun/owner-review-final/dev-r2-owner-review-final-20261017/summary.json)
- [用户明确修订记录](../../reports/rewrite-m3-rerun/owner-review-final/dev-r2-owner-review-final-20261017/specific-correction.json)
- [最终裁决输入](../../reports/rewrite-m3-rerun/owner-review-final/dev-r2-owner-review-final-20261017/decisions-final.jsonl)
- [现有入口的分组及成对统计](../../reports/rewrite-m3-rerun/owner-review-final/dev-r2-owner-review-final-20261017/adjudication/summary.json)
- [服务器验证记录](../../reports/rewrite-m3-rerun/owner-review-final/dev-r2-owner-review-final-20261017/validation.json)

最终 manifest SHA256：`a8068e7081673153a8bdc35c3260d8fbb87cd4bebac1bce6e18c8748ecdecddc`。最终证据归档 SHA256：`2655f1461accbd56c2d8db9646fa9dfcce27952a4e58f0e643248b816a496b14`。私有题目、来源、答案及裁决明细只在服务器和本地 Git 忽略目录保存，不提交 Git。

历史证据仍保留：前版 manifest `32ced2e9…34901`、归档 `03db20ac…f2f4b`；本次修订通过引用关系追加，不覆写前版。
