# 问答异步执行与页面恢复交付记录

日期：2026-10-23。状态：实现与自动验证完成，已推送功能分支并部署正式服务器，待人工验收；自动验证未执行付费模型调用。发布详情见 [部署记录](chat-reconnect-deployment-20261023.md)。

对应 [需求规格](spec-chat-reconnect.md) 与 [已确认技术方案](technical-plan-chat-reconnect.md)。分支：`codex/chat-async-polling-20261023`。

## 实际行为

- 提交问题后返回任务消息 ID，API 生命周期持有并执行问答任务；浏览器不再拥有任务生命周期。
- 每秒把已生成全文合并写入现有 `messages.answer`，前端串行轮询原会话并替换全文，不新增数据表或结构迁移。
- 切换页面或会话、刷新、断网和关闭标签页均不取消已受理任务。恢复查看使用原消息；提交回执丢失也沿用原请求编号，不重复推理。
- 正文生成中标为草稿，引用校验通过后保存正式答案、引用及终态；失败、停止或重启清空未完成正文，草稿不进入后续模型历史。
- 主动停止、权限失效、超时或 API 关闭仍取消真实任务及上游 HTTP。停止等待受理交接完成，连续撤权不会再次打断终态清理，迟到错误不能覆盖完成结果。
- 删除旧 HTTP SSE 执行适配、前端 delta 拼接及失效协议测试，保留图内事件供执行与离线评测使用。

## 接口切换

| 接口 | 当前实现 |
| --- | --- |
| `POST /api/v1/chat` | 请求字段不变；首次返回 202 JSON，同编号同问题返回 200 原消息，同编号不同问题返回 409 |
| `GET /api/v1/sessions/{session_id}/messages` | 复用历史接口；生成中正文为草稿，终态为正式结果；逐次校验所有权和来源权限 |
| `POST /api/v1/sessions/{session_id}/messages/{message_id}/stop` | 取消对应任务，返回实际状态；重复停止安全，已完成答案不会清空 |

受理/停止回执见 `rewrite/contracts/chat-receipt-v1.schema.json`，HTTP 接口见生成后的 `rewrite/contracts/openapi.json`。POST 问答不再返回 SSE，前后端必须协调发布；本次已一起更新正式 API 与 Web。独立外部客户端需要同步修改；未核查生产访问日志中是否存在这类客户端。

## 验证结果

全部业务集成验证使用独立 PostgreSQL/pgvector、一次性测试数据库和本机合成模型，没有访问正式模型网关。

| 验证 | 结果 |
| --- | --- |
| Ruff 与 mypy | 通过；mypy 检查 71 个源文件 |
| 完整后端回归 | 155 项通过，96.99 秒；Windows 启用 `PYTHONUTF8=1` |
| 最后补充边界回归 | 20 项通过，26.56 秒；含受理中停止、连续撤权、无页面保存进度、回执丢失、发送失败、重复受理、存储失败、关闭、重启恢复与权限屏障；与完整回归有重叠，不累加数量 |
| 前端测试 | 6 项通过：全文替换、在途刷新串行补查、切换丢弃旧响应、临时网络故障、登录失效、待确认请求按用户隔离 |
| TypeScript 与 Vite 生产构建 | 通过 |
| 真实浏览器联测 | 25 项通过，页面异常为 0；覆盖 API、Vue、独立入库 Worker、登录、文件引用、权限及移动端 |
| 恢复期间生成次数 | 切页、切换会话、刷新、离线恢复、关闭重开标签页、丢失受理回执六个场景，各仅生成一次 |
| 服务器待发布镜像 | 使用实际 API/Web 镜像、独立临时数据库和合成模型，重复通过 25 项浏览器检查，页面错误 0 |
| 发布后公网与容器 | 公网 7 项检查、页面错误 0；API/Web/数据库 healthy、Worker running；运行容器的 JSON 回执、查询和停止接口定义正确 |

浏览器检查清单：`data/rewrite-m1-qa/browser-check.json`；恢复页面截图：`data/rewrite-m1-qa/chat-recovered-desktop.png`。它们是忽略提交的本机合成验证产物。

复现命令（先启动独立测试 PostgreSQL，勿使用业务库）：

```powershell
$env:PYTHONUTF8 = '1'
.\rewrite\backend\.venv\Scripts\python.exe rewrite/scripts/test_postgres.py start
.\rewrite\backend\.venv\Scripts\python.exe -m pytest rewrite/backend/tests -q --tb=short
npm.cmd --prefix rewrite/frontend test
npm.cmd --prefix rewrite/frontend run build
$env:HD_BROWSER = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
.\rewrite\backend\.venv\Scripts\python.exe rewrite/scripts/verify_browser.py
```

## 限制与人工验收

- 仍只支持单 API 进程；API 重启把未完成问答标为中断，不自动续跑。不新增独立问答 Worker、消息队列或跨设备恢复承诺。
- 前端不是逐字推送，进度写入和查询各约 1 秒，展示还受网络和处理耗时影响；长会话轮询传输整个消息列表。
- 原有令牌、权限、时限与模型预算仍有效；发起令牌失效可能终止执行。
- 本轮验证证明恢复与生命周期行为，不代表真实模型答案质量已经重新验收。
- 已按用户授权完成生产发布；待用户人工检查恢复与停止体验。独立旧客户端需要同步升级；服务器和 API 重启仍不支持问答自动续跑。
