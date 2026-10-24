import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langsmith import tracing_context

from helpdesk.application.model_session import ModelSession
from helpdesk.application.retrieval import bm25_order, citation_indices, round_robin, rrf, select_context
from helpdesk.application.trace import TraceObserver
from helpdesk.domain.contracts import PublicEvent
from helpdesk.domain.errors import AppError
from helpdesk.domain.models import Row
from helpdesk.domain.rag import BoundScope, Chunk, ScopeDecision, Sufficiency
from helpdesk.ports.chat import ChatRunInput
from helpdesk.ports.corpus import AuthorizedCorpus

PROMPTS: dict[str, str] = json.loads(
    files("helpdesk.assets").joinpath("prompts.json").read_text(encoding="utf-8")
)
PROMPT_HASHES = {key: hashlib.sha256(text.encode()).hexdigest() for key, text in PROMPTS.items()}


@dataclass(frozen=True)
class Outcome:
    status: str
    answer: str
    citations: list[Row]
    trace: Row


class State(TypedDict, total=False):
    query: str
    behavior: str
    kb_ids: list[str]
    required: list[str]
    queries: list[str]
    channels: dict[str, list[list[Chunk]]]
    fused: list[Chunk]
    ranked: list[Chunk]
    selected: list[Chunk]
    answer: str
    citations: list[Row]
    trace: Row
    outcome: Outcome
    clarification_reason: str


@dataclass(frozen=True)
class Context:
    run: ChatRunInput
    scope: BoundScope
    models: ModelSession
    corpus: AuthorizedCorpus
    cpu: ProcessPoolExecutor
    cpu_slots: asyncio.Semaphore
    retrieval_variant: str = "D"
    observer: TraceObserver = field(default_factory=TraceObserver)


def emit(runtime: Runtime[Context], event: str, data: Row) -> None:
    runtime.stream_writer({"event": event, "data": data})


async def admit(state: State, runtime: Runtime[Context]) -> State:
    # 消息幂等和范围绑定已由入口事务完成，图内再次验证运行上下文。
    await runtime.context.models.guard()
    return {"trace": {"scope_hash": runtime.context.scope.scope_hash}}


async def resolve_scope(state: State, runtime: Runtime[Context]) -> State:
    ctx = runtime.context
    scope = await ctx.corpus.metadata(ctx.run.principal, ctx.scope)
    emit(runtime, "status", {"stage": "understanding", "message": "正在确认问题范围"})
    if not scope.kb_ids or not scope.version_ids:
        return {"behavior": "refuse", "kb_ids": [], "required": []}
    query = state["query"]
    readable = {kb["id"] for kb in scope.knowledge_bases}
    metadata = list(scope.knowledge_bases)
    if len(ctx.scope.kb_ids) == 1:
        if ctx.run.history:
            rewrite = await ctx.models.structured(
                "rewrite", PROMPTS["rewrite"], {"question": query, "history": list(ctx.run.history)}
            )
            value = rewrite.get("query")
            if isinstance(value, str) and value.strip() and len(value) <= 8000:
                query = value
        organization = scope.knowledge_bases[0]["organization"]
        return {
            "query": f"适用业务范围：{organization}\n问题：{query}",
            "behavior": "search",
            "kb_ids": list(ctx.scope.kb_ids),
            "required": [],
        }
    result = await ctx.models.structured(
        "scope",
        PROMPTS["scope"],
        {
            "question": query,
            "history": list(ctx.run.history),
            "available_knowledge_bases": [
                {key: kb[key] for key in ("id", "name", "organization")} for kb in metadata
            ],
        },
    )
    try:
        decision = ScopeDecision.model_validate(result)
    except ValueError as error:
        raise AppError("invalid_scope_decision", 502, "范围判断格式无效") from error
    if not set(decision.kb_ids + decision.option_ids).issubset(readable):
        raise AppError("invalid_scope_decision", 502, "范围判断越出授权范围")
    ids = [key for key in ctx.scope.kb_ids if key in decision.kb_ids] or list(ctx.scope.kb_ids)
    required = [key for key in ids if key in readable] if decision.requires_all else []
    if len(required) > 5:
        return {
            "behavior": "clarify", "kb_ids": ids, "required": [],
            "clarification_reason": "too_many_topics",
        }
    return {
        "query": decision.query,
        "behavior": decision.behavior,
        "kb_ids": ids,
        "required": required,
    }


async def plan_query(state: State, runtime: Runtime[Context]) -> State:
    query = state["query"]
    queries = [query]
    split = {"attempted": False, "accepted": False}
    if runtime.context.run.server_strategy_id == "split-v1" and (
        state.get("required") or re.search(r"同时|分别|以及|并且|比较|区别|两篇|两库", query)
    ):
        split["attempted"] = True
        try:
            result = await runtime.context.models.structured("split", PROMPTS["split"], {"question": query})
            parts = result.get("subqueries")
            if (
                isinstance(parts, list)
                and len(parts) == 2
                and len(set(map(str, parts))) == 2
                and all(
                    isinstance(p, str) and p.strip() and len(p) <= 2000 and p in query and p != query
                    for p in parts
                )
            ):
                queries.extend(f"原问题及适用条件：{query}\n重点子任务：{p}" for p in parts)
                split["accepted"] = True
        except AppError as error:
            if error.code != "invalid_provider_response":
                raise
    return {
        "queries": queries,
        "trace": {
            **state.get("trace", {}),
            "split": split,
            "query_hashes": [hashlib.sha256(q.encode()).hexdigest() for q in queries],
        },
    }


async def lexical_order(ctx: Context, query: str, texts: list[str], ids: list[str]) -> list[int]:
    await ctx.cpu_slots.acquire()
    try:
        future = asyncio.get_running_loop().run_in_executor(ctx.cpu, bm25_order, query, texts, ids)
    except BaseException:
        ctx.cpu_slots.release()
        raise
    # 取消协程不会终止进程里的计算；实际计算结束后才归还容量，防止超时排队失控。
    future.add_done_callback(lambda _: ctx.cpu_slots.release())
    return await asyncio.shield(future)


async def retrieve(state: State, runtime: Runtime[Context]) -> State:
    ctx = runtime.context
    emit(runtime, "status", {"stage": "retrieving", "message": "正在检索授权资料"})
    channels: dict[str, list[list[Chunk]]] = {key: [] for key in state["kb_ids"]}
    corpus = {
        kb_id: await ctx.corpus.chunks(ctx.run.principal, ctx.scope, kb_id) for kb_id in state["kb_ids"]
    }
    if not any(corpus.values()):
        return {"channels": channels, "trace": {**state["trace"], "candidate_ids": {}}}
    vectors = {}
    if ctx.retrieval_variant != "A":
        for query in state["queries"]:
            vectors[query] = (await ctx.models.embed([query]))[0]
    for kb_id in state["kb_ids"]:
        chunks = corpus[kb_id]
        if not chunks:
            continue
        for query in state["queries"]:
            if ctx.retrieval_variant != "B":
                order = await lexical_order(ctx, query, [c.text for c in chunks], [c.id for c in chunks])
                channels[kb_id].append([chunks[i] for i in order])
            if ctx.retrieval_variant != "A":
                vector = await ctx.corpus.vector(ctx.run.principal, ctx.scope, kb_id, vectors[query])
                channels[kb_id].append(vector)
    trace = {
        **state["trace"],
        "candidate_ids": {k: [[c.id for c in channel] for channel in value] for k, value in channels.items()},
    }
    return {"channels": channels, "trace": trace}


async def fuse(state: State, runtime: Runtime[Context]) -> State:
    groups = [rrf(state["channels"][k]) for k in state["kb_ids"]]
    fused = round_robin(groups)
    return {"fused": fused, "trace": {**state["trace"], "fused_ids": [c.id for c in fused]}}


async def rerank(state: State, runtime: Runtime[Context]) -> State:
    fused = state["fused"]
    if runtime.context.retrieval_variant != "D":
        return {"ranked": fused, "trace": {**state["trace"], "reranked_ids": [c.id for c in fused]}}
    if not fused:
        return {"ranked": []}
    order = await runtime.context.models.rank(
        state["query"],
        [f"知识库：{c.kb_name}；适用业务范围：{c.organization}；文档：{c.title}\n{c.text}" for c in fused],
    )
    if sorted(order) != list(range(len(fused))):
        raise AppError("invalid_rerank", 502, "重排结果不完整")
    ranked = [fused[i] for i in order]
    return {"ranked": ranked, "trace": {**state["trace"], "reranked_ids": [c.id for c in ranked]}}


async def choose_context(state: State, runtime: Runtime[Context]) -> State:
    selected = select_context(state["ranked"], state["required"])
    return {
        "selected": selected,
        "trace": {
            **state["trace"],
            "context_ids": [c.id for c in selected],
            "context_hashes": [c.content_hash for c in selected],
            "context_characters": sum(len(c.text) for c in selected),
        },
    }


def evidence(state: State) -> list[Row]:
    return [chunk.evidence(f"C{i + 1}") for i, chunk in enumerate(state["selected"])]


def model_evidence(state: State) -> list[Row]:
    # 冻结模型的可见字段；稳定 ID 留给业务引用和诊断，不混入实验提示。
    return [
        {
            "label": f"C{i + 1}",
            "text": c.text,
            "title": c.title,
            "section": c.locator.get("section", ""),
            "knowledge_base": c.kb_name,
            "organization": c.organization,
        }
        for i, c in enumerate(state["selected"])
    ]


async def sufficiency(state: State, runtime: Runtime[Context]) -> State:
    selected = state["selected"]
    if not selected or not set(state["required"]).issubset({c.kb_id for c in selected}):
        return {"behavior": "refuse"}
    result = await runtime.context.models.structured(
        "sufficiency",
        PROMPTS["sufficiency"],
        {
            "question": state["query"],
            "evidence": model_evidence(state),
            "required_organizations": [
                kb["organization"]
                for kb in runtime.context.scope.knowledge_bases
                if kb["id"] in state["required"]
            ],
        },
        limit=2048,
    )
    try:
        decision = Sufficiency.model_validate(result)
    except ValueError as error:
        raise AppError("invalid_sufficiency", 502, "证据判断格式无效") from error
    allowed = {f"C{i + 1}" for i in range(len(selected))}
    if not set(decision.evidence_labels).issubset(allowed) or (
        decision.behavior == "answer" and not decision.evidence_labels
    ):
        raise AppError("invalid_sufficiency", 502, "证据判断引用无效")
    return {"behavior": decision.behavior}


async def generate(state: State, runtime: Runtime[Context]) -> State:
    emit(
        runtime,
        "evidence",
        {
            "behavior": "answer",
            "snapshot_ids": [s["id"] for s in runtime.context.scope.snapshots],
            "evidence": [
                {"label": f"C{i + 1}", "chunk_id": c.id, "version_id": c.version_id}
                for i, c in enumerate(state["selected"])
            ],
        },
    )
    system = PROMPTS["answer"] + PROMPTS["comparison_suffix"]
    answer = ""
    async for delta in runtime.context.models.generate(
        system, {"question": state["query"], "evidence": model_evidence(state)}
    ):
        answer += delta
        emit(runtime, "answer_delta", {"text": delta})
    return {"answer": answer}


async def validate_citations(state: State, runtime: Runtime[Context]) -> State:
    indices = citation_indices(state["answer"], len(state["selected"]), state["required"], state["selected"])
    citations = [evidence(state)[i] for i in indices]
    emit(runtime, "citations", {"citations": citations})
    return {"citations": citations}


async def limited(state: State, runtime: Runtime[Context]) -> State:
    clarify = state["behavior"] == "clarify"
    ids = state.get("kb_ids", list(runtime.context.scope.kb_ids))
    topics = list(dict.fromkeys(k["name"] for k in runtime.context.scope.knowledge_bases if k["id"] in ids))
    if not clarify:
        message = "当前授权的推送资料不足，无法回答该问题。"
    elif state.get("clarification_reason") == "too_many_topics":
        # 范围过大时应缩小主题，而不是要求补充与问题无关的业务条件。
        message = "本次需要比较的资料主题超过 5 个，请缩小到最多 5 个主题后再提问。"
    else:
        message = "请补充推送所属应用、终端平台或通道、发生时间及具体异常现象。"
    if clarify and topics:
        message += "当前可查询主题：" + "、".join(topics) + "。"
    emit(
        runtime,
        "evidence",
        {
            "behavior": state["behavior"],
            "snapshot_ids": [s["id"] for s in runtime.context.scope.snapshots],
            "evidence": [],
        },
    )
    emit(runtime, "refusal", {"behavior": state["behavior"], "message": message})
    return {"answer": message, "citations": []}


async def finalize(state: State, runtime: Runtime[Context]) -> State:
    status = (
        "completed"
        if state["behavior"] == "answer"
        else "clarify"
        if state["behavior"] == "clarify"
        else "refused"
    )
    return {
        "outcome": Outcome(
            status,
            state.get("answer", ""),
            state.get("citations", []),
            {
                **state.get("trace", {}),
                "prompt_hashes": PROMPT_HASHES,
                "strategy": runtime.context.run.server_strategy_id,
                "routed_kb_ids": state.get("kb_ids", []),
                "required_kb_ids": state.get("required", []),
            },
        )
    }


class GraphNode(Protocol):
    def __call__(self, state: State, *, runtime: Runtime[Context]) -> Awaitable[State]: ...


def observed_node(name: str, node: Callable[[State, Runtime[Context]], Awaitable[State]]) -> GraphNode:
    async def invoke(state: State, *, runtime: Runtime[Context]) -> State:
        observer = runtime.context.observer
        observer.enter(name)
        try:
            return await node(state, runtime)
        except BaseException:
            observer.fail(name)
            raise

    return invoke


def build_graph() -> Any:
    graph = StateGraph(State, context_schema=Context)
    nodes = {
        "admit": admit,
        "resolve_scope": resolve_scope,
        "plan_query": plan_query,
        "retrieve": retrieve,
        "fuse": fuse,
        "rerank": rerank,
        "select_context": choose_context,
        "sufficiency": sufficiency,
        "generate_stream": generate,
        "validate_citations": validate_citations,
        "limited": limited,
        "finalize": finalize,
    }
    for name, node in nodes.items():
        graph.add_node(name, observed_node(name, node))
    graph.add_edge(START, "admit")
    graph.add_edge("admit", "resolve_scope")
    graph.add_conditional_edges(
        "resolve_scope",
        lambda s: "plan_query" if s["behavior"] == "search" else "limited",
        {"plan_query": "plan_query", "limited": "limited"},
    )
    for before, after in [
        ("plan_query", "retrieve"),
        ("retrieve", "fuse"),
        ("fuse", "rerank"),
        ("rerank", "select_context"),
        ("select_context", "sufficiency"),
        ("generate_stream", "validate_citations"),
        ("validate_citations", "finalize"),
        ("limited", "finalize"),
    ]:
        graph.add_edge(before, after)
    graph.add_conditional_edges(
        "sufficiency",
        lambda s: "generate_stream" if s["behavior"] == "answer" else "limited",
        {"generate_stream": "generate_stream", "limited": "limited"},
    )
    graph.add_edge("finalize", END)
    return graph.compile()


COMPILED = build_graph()


class GraphRunner:
    def __init__(self, context: Context) -> None:
        self.context = context
        self.outcome: Outcome | None = None

    def diagnostics(self) -> Row:
        return self.context.observer.snapshot(self.context.models.attempt_details)

    async def events(self) -> AsyncIterator[PublicEvent]:
        seq = 0
        with tracing_context(enabled=False):
            async for mode, part in COMPILED.astream(
                {"query": self.context.run.question},
                context=self.context,
                config={"recursion_limit": 48, "callbacks": []},
                stream_mode=["custom", "updates"],
            ):
                if mode == "updates":
                    for name, update in part.items():
                        if isinstance(update, dict) and name in self.context.observer.nodes:
                            self.context.observer.update(name, update)
                    if "finalize" in part:
                        self.outcome = part["finalize"]["outcome"]
                elif mode == "custom":
                    seq += 1
                    yield PublicEvent(
                        event=part["event"],
                        data=part["data"],
                        seq=seq,
                        request_id=self.context.run.request_id,
                        message_id=self.context.run.message_id,
                    )
