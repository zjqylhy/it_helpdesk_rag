"""只收集阶段状态和有限标识，禁止把节点完整 state 当作诊断日志。"""

import copy

from helpdesk.domain.models import Row

NODES = (
    "admit",
    "resolve_scope",
    "plan_query",
    "retrieve",
    "fuse",
    "rerank",
    "select_context",
    "sufficiency",
    "generate_stream",
    "validate_citations",
    "limited",
    "finalize",
)
TRACE_KEYS = {
    "scope_hash",
    "split",
    "query_hashes",
    "candidate_ids",
    "fused_ids",
    "reranked_ids",
    "context_ids",
    "context_hashes",
    "context_characters",
    "prompt_hashes",
    "strategy",
    "routed_kb_ids",
    "required_kb_ids",
}


class TraceObserver:
    def __init__(self) -> None:
        self.nodes = dict.fromkeys(NODES, "not_reached")
        self.trace: Row = {}

    def enter(self, name: str) -> None:
        self.nodes[name] = "unknown"

    def fail(self, name: str) -> None:
        self.nodes[name] = "failed"

    def update(self, name: str, value: Row) -> None:
        self.nodes[name] = "observed"
        source = value.get("trace", {})
        if name == "finalize":
            source = getattr(value.get("outcome"), "trace", {})
        self.trace.update({k: copy.deepcopy(v) for k, v in source.items() if k in TRACE_KEYS})
        if name == "resolve_scope":
            self.trace.update(
                routed_kb_ids=value.get("kb_ids", []), required_kb_ids=value.get("required", [])
            )
        if name == "rerank" and self.nodes["fuse"] == "observed":
            self.trace.setdefault("reranked_ids", [])

    def snapshot(self, attempts: list[Row]) -> Row:
        trace = copy.deepcopy(self.trace)
        if any(a["stage"] == "split" for a in attempts) and "split" not in trace:
            trace["split"] = {"attempted": True, "accepted": None}
        trace["node_observations"] = dict(self.nodes)
        trace["trace_complete"] = self.nodes["finalize"] == "observed"
        return trace
