"""阶段是否被观测与召回数值分开；错误前已完成的阶段仍有评测价值。"""

from helpdesk.application.evaluation import EvalCase, recall
from helpdesk.domain.models import Row

STAGES = {
    "candidates": ("retrieve", "candidate_ids"),
    "fused": ("fuse", "fused_ids"),
    "reranked": ("rerank", "reranked_ids"),
    "context": ("select_context", "context_ids"),
}


def coverage_metrics(case: EvalCase, trace: Row, chunks: list[Row]) -> Row:
    result: Row = {}
    for stage, (node, field) in STAGES.items():
        observation = trace.get("node_observations", {}).get(node, "not_reached")
        metric: Row = {
            "observation": observation,
            "covered_groups": None,
            "required_groups": len(case.evidence_groups),
            "recall": None,
            "full": None,
        }
        if observation == "observed" and field in trace:
            ids = trace[field]
            if stage == "candidates":
                ids = list({i for channels in ids.values() for channel in channels for i in channel})
            metric.update(recall(case, ids, chunks))
        result[stage + "_coverage"] = metric
    rank = dict(result["reranked_coverage"])
    if rank["observation"] == "observed" and "reranked_ids" in trace:
        rank.update(recall(case, trace["reranked_ids"][:5], chunks))
    result["recall_at_5"] = rank
    return result


def retrieval_summary(rows: list[Row], cases: dict[str, EvalCase]) -> Row:
    answerable = [r for r in rows if cases[r["case_id"]].expected_behavior == "answer"]
    result = {}
    for field in [*(s + "_coverage" for s in STAGES), "recall_at_5"]:
        observed = [r[field] for r in answerable if r.get(field, {}).get("recall") is not None]
        result[field] = {
            "recorded_answerable": len(answerable),
            "observed": len(observed),
            "unobserved": len(answerable) - len(observed),
            "mean_recall_observed": sum(m["recall"] for m in observed) / len(observed) if observed else None,
            "fully_covered_observed": sum(m["full"] for m in observed),
        }
    return result
