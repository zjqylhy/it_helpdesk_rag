# 全栈架构与流程图

日期：2026-10-17。当前实现绑定代码快照 `ae16c6c43f0f1b698676d02a15ad0a34bfbec178`；目标图全部待实现；后端采用全 Python，现状图保留实际已有的 Go 服务。

[打开全部图表查看器](index.html)：可切换七图，缩放、适应宽度，直接查看 SVG 和 Mermaid 源码。

| 范围 | 图 | 可编辑源码 |
| --- | --- | --- |
| 当前已有 | [整体架构 SVG](current-architecture.svg) · [交互版](../../../.archify/architecture-current-system-20261017-035056/architecture.html) | [Mermaid](current-architecture.mmd) |
| 当前已有 | [问答流程](current-chat.svg) | [Mermaid](current-chat.mmd) |
| 当前已有 | [入库与版本发布](current-ingestion.svg) | [Mermaid](current-ingestion.mmd) |
| 当前已有 | [撤权与历史遮蔽](current-authorization.svg) | [Mermaid](current-authorization.mmd) |
| 当前已有 | [冻结成对评测](current-evaluation.svg) | [Mermaid](current-evaluation.mmd) |
| 目标待实现 | [全 Python 后端与 Vue 分层架构](target-architecture.svg) | [Mermaid](target-architecture.mmd) |
| 目标待实现 | [完整 LangGraph 执行流程](target-workflow.svg) | [Mermaid](target-workflow.mmd) |

Mermaid 源码从现状文档和目标技术方案提取，SVG 通过独立 Mermaid CLI 与本机 Edge 渲染。架构交互版另有代码引用与自动校验记录。复杂目标图的交互布局尝试未通过，未将其作为交付链接；完整 SVG 保留了全部节点、条件和回边。

图表与文档中的业务规则以 [新规格](../spec.md)、[技术方案](../technical-plan.md) 和 [契约](../contracts.md) 为准。图中的“最终发布”在完整检查模式下仅发送已通过检查的正文；基线直接流式路径保留作独立对照，不可混算首字延迟。
