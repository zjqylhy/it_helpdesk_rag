# 评测集格式与维护

文件为 UTF-8 JSONL，每行一题。以下是格式示例，版本 ID 与 quote 需要替换成实际导出快照中的值；它不是可用黄金样本。

```json
{"id":"vpn-001","case_revision":1,"status":"review","split":"dev","theme":"vpn-server-configuration","tags":["single-document"],"knowledge_base":"KB_ID","corpus_version":"SNAPSHOT_ID","question":"该组织的 VPN 如何配置？","history":[],"expected_behavior":"answer","reference_answer":"依据原文填写","required_facts":["依据原文填写必要事实"],"forbidden_facts":[],"evidence_groups":[[{"source_version":"VERSION_ID","locator":{"paragraph":3,"section":"客户端配置"},"quote":"原文节选"}]],"change_reason":"首次标注","review_record":{}}
```

`status` 支持 draft、review、active、retired。有效样本必须有审核人和审核时间；停用题目在发布时也要审核。`split` 为 dev/test，同一 `theme` 不能跨划分。文档版本、知识快照和数据集发布版本分别管理。

证据锚点引用原文版本、页码/段落/章节及原样节选。同组来源可互相替代，不同组代表必须组合的证据。重复出现的节选需要在 locator 中提供 `start` 字符位置。切片的 start/end 是段落内字符区间；召回按区间合并判断，重叠切片不会重复计分。

## 人工修改

1. 用 `draft RELEASE/cases.jsonl --out NEW_DRAFT.jsonl` 创建草稿。
2. 将需要修改的完整样本另存为 JSON，保留 id，将 case_revision 增加 1，填写 change_reason；用 `edit DRAFT --case CASE.json --out NEXT_DRAFT` 应用修改或新增。
3. 用 `validate DRAFT --corpus CORPUS.json` 核对原文证据。
4. 逐题人工审查问题、必要事实、预期行为和来源，用 `review` 写入审核记录。
5. 用 `publish` 创建新的发布目录；旧目录不可覆盖。

原文版本更新时，未受事实变动影响的题目仍需绑定新 corpus_version 并核对出处。评测可以复现旧快照，旧成绩只描述旧语料；正式验收必须使用当前语料匹配的发布集。不能将新旧数据集成绩差异直接视为算法提升。

## AI 辅助维护

先导出更新后的语料，再执行：

```powershell
uv run rag-eval diff OLD_DATASET.jsonl --old OLD_CORPUS.json --new NEW_CORPUS.json --out IMPACT.json
uv run rag-eval suggest OLD_DATASET.jsonl --old OLD_CORPUS.json --new NEW_CORPUS.json --ids CASE_1,CASE_2 --out PROPOSALS.json --max-calls 20
```

`diff` 输出文本差异与待检查题目，保守包含来源变更题、拒答/澄清题、跨文档题及可能受到新增冲突影响的题目。`suggest` 使用旧/新原文提出 keep、reanchor、revise、retire 建议，并验证新证据节选；输入过长会由模型接口明确失败，首期请按小规模知识库或分批语料处理。

建议文件保留 before/after。人工检查后将 after 保存成单题 JSON，再使用 `edit`、`validate`、`review`、`publish`。AI 不直接更新已发布数据集，也不使用被测 RAG 回答充当标准答案。新增题可用 `generate` 创建少量候选后人工审核，拒答、冲突和多轮场景需要人工补充。

`generate` 显式提供候选 JSON Schema，expected_behavior 仅允许 answer，使用原文段落而非重复的切片内容作为输入。输出长度随题量增长，上限 16384 token，不改变在线问答默认预算；可用 `--max-calls 20` 限制 HTTP 调用次数（包含重试）。不完整 JSON、错误枚举、重复 ID 或不匹配的原文证据都会拒绝保存；生成结果始终为 review，清除模型提供的审核记录，不能自动成为已发布黄金集。

文档变更后，即使只修改少数题，也应在新的完整冻结集上重跑旧/新检索策略，建立新基线。维护成本由模型调用用量和受影响题目的人工审核工作量组成；已完成少量真实素材的新题生成，但尚未测量文档更新后的维护工时，不能给出工时或金额承诺。

## 人工裁决

将逐题裁决保存为 JSONL：

```json
{"id":"vpn-001","correct":true,"reviewer":"审核人","reason":"必要事实齐全，适用组织和出处正确"}
```

```powershell
uv run rag-eval adjudicate --run RUN_DIRECTORY --decisions DECISIONS.jsonl --out REVIEWED_RUN_DIRECTORY
```

人工结果与自动评分同时保留。正式答案正确率要求所有可回答题都有人工判定，且失败、拒答或截断不能被标为成功回答。正式测试集已按用户最新指令恢复为 200 题，首轮最终方案 D 的 200 题需全量审核；后续变化/失败题与至少 40 条分层抽样审核的要求见技术方案。测试题按事实主题与 30 题开发集隔离，不能以开发题改写凑足测试规模。

题目与原文的审核不等于本次实际答案的裁决；每轮生成都要保留独立审核状态。必要事实宜逐项列出，避免把账号、验证码、首次绑定和操作设备等条件混成一个事实。评委必须核对每项事实的全部条件；引用原文含有某条件，不能替代回答明确覆盖该条件。

只复评已保存答案可使用 `judge DATASET --run OLD_RUN --out NEW_RUN --max-calls 40`。旧生成代码与提示版本保留，另记 `judge_commit`、`judge_prompt`、`judge_llm` 和来源清单校验值。当前事实评委提示版本为 `facts-v2`。不传 `--ragas` 时保留原忠实度结果，不重新计算该指标。

## 多知识库开发集

各知识库分别绑定快照、校验并发布。`eval/private/releases/dev-30-v1/index.json` 记录 7 个成员发布集与整体校验值，`all-cases.jsonl` 保存 30 题集合；单次 `run` 使用一个成员的题目与语料。以下命令从 `rag` 目录执行，输出必须为新目录：

```powershell
$evalRoot = (Resolve-Path ../eval/private/releases/dev-30-v1).Path
$evalIndex = Get-Content (Join-Path $evalRoot index.json) -Raw | ConvertFrom-Json
foreach ($member in $evalIndex.members) {
    $evalCases = Join-Path $evalRoot $member.cases
    $evalCorpus = Join-Path $evalRoot $member.corpus
    uv run rag-eval run $evalCases --corpus $evalCorpus --variant all --out "../reports/dev-replay-retrieval/$($member.slug)" --max-calls 40
    if ($LASTEXITCODE -ne 0) { throw "检索运行失败：$($member.slug)" }
    uv run rag-eval run $evalCases --corpus $evalCorpus --mode end-to-end --variant D --judge --ragas --out "../reports/dev-replay-answers/$($member.slug)" --max-calls 60
    if ($LASTEXITCODE -ne 0) { throw "端到端运行失败：$($member.slug)" }
}
```

整体汇总按逐题记录及对应分母计算，不能平均各库的正确率；来源、数据集、代码和模型清单均须保留。30 题为开发集，可用于定位问题和调整提示；正式测试题须按事实主题独立划分。

## 二百题测试集审核与重放

`eval/private/test-candidates-v1/` 保存 7 库候选、对应固定语料、整体清单及 `all-cases.jsonl`。`review.html` 可在本机浏览器打开，按知识库/场景筛选或搜索题目、必要事实和原文，展开后检查固定历史、标准、定位及原始来源；页面不自动发布或上传内容。`review.json` 提供同样内容的结构化版本。

候选为单文档 100、多轮 30、同库多文档 30、无答案 25、条件/适用范围 15；预期行为分别为 answer 160、refuse 25、clarify 15。原候选包保留 test/review 状态。用户于 2026-10-10 确认题目和原文审核后，逐题写入审核记录，按库冻结为 `eval/private/releases/test-200-v1/`，200 题均为 active；整体 `index.json` 保存确认原话、审核包校验值和成员快照清单。来源段落、版本和快照已校验，另用开发题与候选合并文件检查跨划分主题；排除开发证据段落及已知的同事实重复表述。程序检查不能替代人工对语义重叠和无答案范围的审查。

本套覆盖 140 个事实主题；多轮与组合场景共享部分测试侧事实，200 条题目不等于 200 个独立事实。正式报告需保留分库、分场景结果和事实依赖，避免仅看总体均值。只有 Windows 库含多个原文，30 条同库多文档候选因此都在该库。差旅题限定历史文件年份，不确认现行效力。

用户审核问题及原文后，逐题执行 `review` 并按库冻结新发布版本；未审核题不得混入正式集。原文审核与实际答案裁决仍为两个节点。正式运行预算按批次和模式设置：默认 `--max-calls 200` 是 HTTP 请求上限，200 题不能理解成只需 200 次请求。

本轮正式运行输出在 `reports/test-200-v1/`。四组检索共用每库同一份已持久化改写问题；四组端到端分别重放完整路径并辅助评分，仅 D 计算 Ragas 忠实度。每批独立记录 HTTP 请求数和 token 用量，按逐题记录计算总指标及知识库、场景分组指标。原文审核的“确认”不会自动转为实际答案的人工裁决；正式正确率在答案复核完成前保持 null。

重放时从 `rag` 目录执行以下命令，另选新的输出根目录，保留本轮结果。请求预算按各库题数计算，包含重试；Ragas 异常、截断和执行失败必须保留在报告中。以下为顺序运行示例，调用真实 API 会产生费用：

```powershell
$testRoot = (Resolve-Path ../eval/private/releases/test-200-v1).Path
$testIndex = Get-Content (Join-Path $testRoot index.json) -Raw | ConvertFrom-Json
foreach ($member in $testIndex.members) {
    $testCases = Join-Path $testRoot $member.cases
    $testCorpus = Join-Path $testRoot $member.corpus
    $retrievalBudget = $member.case_count * 6 + 30
    uv run rag-eval run $testCases --corpus $testCorpus --split test --variant all --out "../reports/test-replay/retrieval/$($member.slug)" --max-calls $retrievalBudget
    if ($LASTEXITCODE -ne 0) { throw "检索运行失败：$($member.slug)" }
    foreach ($variant in @('D','A','B','C')) {
        $answerBudget = $member.case_count * $(if ($variant -eq 'D') { 9 } else { 5 }) + 30
        $faithfulnessOption = @()
        if ($variant -eq 'D') { $faithfulnessOption = @('--ragas') }
        uv run rag-eval run $testCases --corpus $testCorpus --split test --mode end-to-end --variant $variant --judge @faithfulnessOption --out "../reports/test-replay/end-to-end/$($member.slug)/$variant" --max-calls $answerBudget
        if ($LASTEXITCODE -ne 0) { throw "问答运行失败：$($member.slug)/$variant" }
    }
}
```

跨库汇总先核对各成员清单与发布集，再合并逐题记录；不能直接平均各库指标。私有报告中的 `comparison-summary.json` 汇总八组结果，`human-review.html/json` 并列题目、标准、黄金出处、D 实际回答及 A/B/C 对照；人工裁决另存新报告，原始答案和自动评分不覆盖。

`proposed-decisions.jsonl` 是依据事实/行为辅助评分生成的 200 条裁决草案，全部为 pending，未填写人工审核人，不直接导入 `adjudicate`。用户核对实际答案后确认草案或按题号修正，再生成包含审核人、理由、确认记录和审核包校验值的正式人工裁决。忠实度或引用语义分歧提示不自动等同于答案错误；本轮有 18 条事实/行为未通过、共 51 条带复核提示。

## 指标解释

- Recall@5 是每题必要证据组覆盖率再取平均；完整覆盖和 MRR 单独报告。
- 自动答案正确率是每题所有必要事实通过且无错误结论的二元比例；评分不齐时报告 null。
- Faithfulness 衡量答案是否受到本次上下文支持，不能替代业务正确率。
- refused 与 clarify 分别评价；全部拒答不会提高可回答题正确率。
- CLI 服务端 TTFT 与页面发送到渲染首字的 TTFT 分别记录，测试替身耗时不代表真实模型耗时。
- 引用编号合法性只证明引用属于本次证据，语义支持需要逐事实评委和人工核对。
