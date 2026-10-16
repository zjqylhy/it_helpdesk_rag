# 全栈领域与接口契约

状态：待确认的实现依据。本文定义新系统的语义，不要求复刻旧函数或文件结构。确认后补齐机器可校验的 JSON Schema/OpenAPI、SQL 迁移和契约夹具；不能把下述设计表视为这些代码文件已经存在。

## 1 领域对象

| 对象 | 必需字段或约束 |
| --- | --- |
| Principal | user_id、role、disabled、usage_kind；只来自可信 Python Identity 用例与数据库，不能信任浏览器透传 |
| AuthorizedScope | 请求的 all/selected 范围、实际 kb_ids、snapshot_members、authorized_version_ids、authorization_revision、scope_hash |
| QueryPlan | 原问题、独立 query、组织/年份条件、tasks、实际 retrieval_queries、query_strategy、prompt_hash |
| Task | task_id、原问题要求、组织/条件、必须回答项；最多两个独立任务，不能将模型臆造要求纳入 |
| Evidence | chunk_id、version_id、document_id、kb_id、原文 text、locator、content_hash；必须属于 AuthorizedScope |
| SelectedContext | 有序证据 ≤5、正文字符 ≤8000、context_hash、最终 C 编号映射 |
| CoverageReport | 每任务状态、支持原文区间、缺失要求、missing_task_ids、coverage_round、context_hash |
| AnswerCheck | 草稿 hash、context_hash、关键陈述位置、引用、支持状态、遗漏项、verdict |
| RequestBudget | request_id、deadline、实际/预留 HTTP 与 Token、阶段上限、已补查/修正轮次 |
| RunManifest | 代码、图版本、模型实际版本、Prompt、分词器、语料/授权/题集哈希、策略、预算、流模式和评分协议 |

业务数据放入有类型的图状态；数据库连接、HTTP 客户端、授权检查器、预算控制器和流输出器放入 Runtime Context，不序列化到模型输入或公开响应。节点只返回允许的状态更新，不持有全局请求状态。

## 2 覆盖结果

```json
{
  "schema": "coverage-v1",
  "round": 0,
  "context_hash": "example-only",
  "tasks": [
    {"task_id": "T1", "status": "covered", "support": [
      {"chunk_id": "example-chunk-1", "version_id": "example-version-1", "start": 10, "end": 30}
    ], "missing_requirements": [], "reason": "所引正文支持该任务要求"},
    {"task_id": "T2", "status": "missing", "support": [],
      "missing_requirements": ["第二项操作的必要前置步骤"], "reason": "没有找到支持片段"}
  ],
  "missing_task_ids": ["T2"]
}
```

状态为 covered/partial/missing/ambiguous/unknown。程序校验全部 task_id 恰好出现一次、ID 所属授权、版本、字符区间、正文长度与 context_hash；没有支持时必须是空数组。covered 不允许有未解决要求；ambiguous 去澄清；unknown 不通过。

候选诊断另输出 candidate_missing/selection_loss/uncertain，含 inspected_stage 与 inspected_ids。只检查融合候选时不能声称原始候选并集都没有证据。输入超预算不得静默截断后冒充完整检查。

## 3 回答检查结果

`answer-check-v1` 必含 draft_hash、context_hash、claims、missing_task_ids、unsupported_claim_ids、invalid_citation_labels、verdict。每个 claim 给出正文字符位置、task_ids、citation_labels、evidence_spans、supported/unsupported/uncertain。

判断范围包括操作路径、顺序、适用组织与时间条件。空 claims 或检查格式非法不能被视为通过；礼貌性文本无需伪造引用。缺少用户未要求且原文也不要求的“常识步骤”不计遗漏。

修正输出重新计算草稿 hash 并重跑检查；覆盖 hash 与检查 hash 必须与最终证据一致。最终引用标签只在最终上下文锁定后分配，候选合并后旧标签全部失效。

## 4 对外 HTTP

| API 组 | 需要重新实现的操作 |
| --- | --- |
| `/api/v1/auth/*` | login/logout/me，Bearer Token；过期与停用账号失效 |
| `/api/v1/knowledge-bases` | 列表、管理员创建、default-search 设置；返回仅可见元数据 |
| `/api/v1/documents` | 上传、列表、追加版本、版本与切片列表、enabled 设置 |
| `/api/v1/ingestion-jobs/:id/retry` | 管理员显式重试失败任务，不能重复创建同一版本任务 |
| `/api/v1/files/:version` | 当前授权下下载对应原文件，受限与不存在统一 404 |
| `/api/v1/sessions` | 创建、列表、归属验证、恢复保存范围 |
| `/api/v1/sessions/:id/messages` | 历史、权限派生信息遮蔽 |
| `/api/v1/chat` | POST SSE；session_id、question、request_id；客户端不选择可信策略/主体 |
| `/api/v1/admin/*` | 用户、停启、组、成员、文档访问配置与审计 |

v1 行为先形成黑盒契约：包括已有字段大小写、日期、空数组、状态码和错误语义。新领域层统一命名，由 HTTP 适配层负责旧字段投影。特别是会话范围错误不能仅因“安全统一”就擅自改成与现有接口不同的状态码；关键要求是不存在与未授权的响应一致。

同 request_id 再次提交不触发第二次模型生成；活动中的同会话新请求返回冲突。新实现不把网络重连解释为可恢复的模型流，客户端明确刷新历史或发起新请求。

## 5 进程内问答契约

FastAPI 适配层完成身份解析后调用 Chat 用例；用例通过类型化 `ChatRunInput` 调用同进程 LangGraph。输入包含 schema_version、request_id、message_id、可信 Principal、authorization_revision、requested_scope、question、过滤后的 history、server_strategy_id、deadline。浏览器只提供第 4 节允许的字段，不能指定可信主体、授权版本、运行策略或预算。

不再提供 Go→Python 内部 HTTP/SSE 接口，也不沿用该接口的内部令牌。身份认证、SQL 授权和模型前校验全部保留；统一语言不等于信任调用方传入的文档 ID。AuthorizedCorpus 在数据库重算范围并绑定版本；文档发布快照不可变，权限实时核验。

图节点经 EventSink 输出白名单事件，由 Chat/SSE 适配层统一编号并持久化最终公开结果。所有模型请求经过新的 ModelPort/Budget 适配器，节点不得自行发裸 HTTP 绕过计量和授权。Python API 与 Worker 不通过 HTTP 调用彼此；任务通过数据库租约与不可变版本协作。

## 6 SSE 协议与终态

保留 `status / evidence / answer_delta / citations / refusal / done / error` 事件，Python Chat/SSE 适配层统一增加 request_id、message_id、递增 seq。UTF-8、CRLF、跨网络包与多行 data 的协议解析有独立夹具；只实现本项目明确协议，不宣称覆盖整个 SSE 标准。

| 事件 | 公开信息 | 约束 |
| --- | --- | --- |
| status | stage、简洁进度文本 | collecting/retrieving/checking 等进度不算正文首字 |
| evidence | 最终快照 ID、可公开行为与最终授权证据 | 不发送候选池、图全状态、内部检查理由或草稿 |
| answer_delta | 已允许公开的 text | B3 不发送草稿或修正中间版本；只发送最终校验通过正文 |
| citations | 最终编号、版本、定位、节选与授权来源 | 必须与最终正文和上下文映射一致 |
| refusal | 资料不足或澄清的公开说明 | 系统故障不能冒充资料不足 |
| done | status、completion_kind、公开耗时/停止原因/用量汇总 | 合法连接至多一个终态；后续不得再有正文 |
| error | 固定错误分类与用户提示 | 不含密钥、提供商原始响应、草稿或受限元数据 |

消息状态保持 generating/completed/refused/clarify/error/interrupted；读取失权内容投影为 restricted。`completion_kind=full|partial` 只在 completed 有效；部分回答需列出未完成的用户要求，不能计整题正确。断开后可以没有网络终态，但数据库必须保存 interrupted/error，不能假记成功。

## 7 存储与事务归属

| 数据或写入动作 | 新实现的唯一业务归属 |
| --- | --- |
| 用户、Token、组、授权、revision、audit | Python Identity/Access 用例 + Repository，同事务更新授权与审计 |
| 会话、消息准入、最终正文与公开引用 | Python Conversation/Chat 用例；单会话互斥与幂等在数据库约束落地 |
| 库配置、文档元数据、enabled、版本创建、任务入队/人工重试 | Python Catalog/Ingestion 用例；原文件原子落盘与失败清理 |
| 任务领取、租约心跳、解析中间结果 | Python Worker Repository，不让 API handler 模拟任务完成 |
| 切片、向量、版本完成、active_version、快照和任务完成 | Python Publication Repository，一个受租约约束的跨表事务 |
| 私有运行记录、模型尝试、检查与草稿诊断 | Python Run Repository；公开 API 只读其允许投影 |
| 不可变评测题/原文/人工裁决、离线请求账本 | 独立评测 ArtifactStore，绑定版本与哈希，不混入业务真数据 |

同表不同操作须限定写列：API 的 Catalog 用例不直接写 chunks/版本完成/active_version；Worker 和问答图节点不写用户、授权或消息公开终态。语言统一后仍按用例与事务归属写入，不能共享一个任意改表的 Store。文档启停与发布争用时，发布事务锁定知识库/文档并按当前 enabled 构造快照，不能复活已停用文档。

新建独立迁移执行入口与迁移锁，不在每次服务启动时运行任意 DDL。优先兼容既有表与 ID，通过增量迁移增加 run_id、运行记录和必要约束；不强制重算向量。若物理 schema 需要变化，先提供明确映射、双向核对和副本恢复验证，再申请切换。

既有用户密码不在启动时无条件重置。空库引导账号通过显式初始化创建，后续密码变更通过管理用例完成；这是新设计的安全行为，应写入升级说明而不是伪装完全无差异。
