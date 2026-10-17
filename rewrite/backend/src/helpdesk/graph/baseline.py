import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langsmith import tracing_context

from helpdesk.application.model_session import ModelSession
from helpdesk.application.retrieval import bm25_order, citation_indices, round_robin, rrf, select_context
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


@dataclass(frozen=True)
class Context:
    run: ChatRunInput
    scope: BoundScope
    models: ModelSession
    corpus: AuthorizedCorpus
    cpu: ProcessPoolExecutor
    cpu_slots: asyncio.Semaphore
    retrieval_variant: str = "D"


def emit(runtime: Runtime[Context], event: str, data: Row) -> None:
    runtime.stream_writer({"event": event, "data": data})


async def admit(state: State, runtime: Runtime[Context]) -> State:
    # 消息幂等和范围绑定已由入口事务完成，图内再次验证运行上下文。
    await runtime.context.models.guard()
    return {"trace": {"scope_hash": runtime.context.scope.scope_hash}}


async def resolve_scope(state: State, runtime: Runtime[Context]) -> State:
    ctx = runtime.context
    emit(runtime, "status", {"stage": "understanding", "message": "正在确认问题范围"})
    if not ctx.scope.kb_ids:
        return {"behavior": "refuse", "kb_ids": [], "required": []}
    query = state["query"]
    if len(ctx.scope.kb_ids) == 1:
        if ctx.run.history:
            rewrite = await ctx.models.structured(
                "rewrite", PROMPTS["rewrite"], {"question": query, "history": list(ctx.run.history)}
            )
            value = rewrite.get("query")
            if isinstance(value, str) and value.strip() and len(value) <= 8000:
                query = value
        return {"query": query, "behavior": "search", "kb_ids": list(ctx.scope.kb_ids), "required": []}
    result = await ctx.models.structured(
        "scope",
        PROMPTS["scope"],
        {
            "question": query,
            "history": list(ctx.run.history),
            "available_knowledge_bases": list(ctx.scope.knowledge_bases),
        },
    )
    try:
        decision = ScopeDecision.model_validate(result)
    except ValueError as error:
        raise AppError("invalid_scope_decision", 502, "范围判断格式无效") from error
    if not set(decision.kb_ids + decision.option_ids).issubset(ctx.scope.kb_ids):
        raise AppError("invalid_scope_decision", 502, "范围判断越出授权范围")
    ids = list(dict.fromkeys(decision.kb_ids)) or list(ctx.scope.kb_ids)
    if decision.requires_all and len(ids) > 5:
        return {"behavior": "clarify", "kb_ids": ids, "required": []}
    return {
        "query": decision.query,
        "behavior": decision.behavior,
        "kb_ids": ids,
        "required": ids if decision.requires_all else [],
    }


async def plan_query(state: State, runtime: Runtime[Context]) -> State:
    query = state["query"]
    queries = [query]
    split = {"attempted": False, "accepted": False}
    if runtime.context.run.server_strategy_id == "split-v1" and re.search(
        r"和|与|以及|分别|同时|对比|比较|区别|；", query
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
                queries.extend(f"完整问题：{query}\n子任务：{p}" for p in parts)
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


async def lexical_order(ctx: Context, query: str, texts: list[str]) -> list[int]:
    await ctx.cpu_slots.acquire()
    try:
        future = asyncio.get_running_loop().run_in_executor(ctx.cpu, bm25_order, query, texts)
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
    vectors = {}
    if ctx.retrieval_variant != "A":
        for query in state["queries"]:
            vectors[query] = (await ctx.models.embed([query]))[0]
    for kb_id in state["kb_ids"]:
        chunks = await ctx.corpus.chunks(ctx.run.principal, ctx.scope, kb_id)
        for query in state["queries"]:
            if ctx.retrieval_variant != "B":
                order = await lexical_order(ctx, query, [c.text for c in chunks])
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
        state["query"], [f"{c.kb_name} {c.organization} {c.title}\n{c.text}" for c in fused]
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


async def sufficiency(state: State, runtime: Runtime[Context]) -> State:
    selected = state["selected"]
    if not selected or not set(state["required"]).issubset({c.kb_id for c in selected}):
        return {"behavior": "refuse"}
    result = await runtime.context.models.structured(
        "sufficiency",
        PROMPTS["sufficiency"],
        {"question": state["query"], "evidence": evidence(state)},
        limit=1536,
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
    system = PROMPTS["answer"] + (PROMPTS["comparison_suffix"] if state["required"] else "")
    answer = ""
    async for delta in runtime.context.models.generate(
        system, {"question": state["query"], "evidence": evidence(state)}
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
    names = list(dict.fromkeys(k["organization"] for k in runtime.context.scope.knowledge_bases))
    message = (
        ("请明确学校、组织或适用条件" + ("，当前可查询：" + "、".join(names) if names else "") + "。")
        if clarify
        else "当前授权资料不足，无法回答该问题。"
    )
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
            },
        )
    }


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
        graph.add_node(name, node)
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

    async def events(self) -> AsyncIterator[PublicEvent]:
        seq = 0
        with tracing_context(enabled=False):
            async for mode, part in COMPILED.astream(
                {"query": self.context.run.question},
                context=self.context,
                config={"recursion_limit": 48, "callbacks": []},
                stream_mode=["custom", "updates"],
            ):
                if mode == "updates" and "finalize" in part:
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
