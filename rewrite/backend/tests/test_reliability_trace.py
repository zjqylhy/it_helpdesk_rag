import asyncio
import json
import time
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context

import pytest
from fakes import FakeModels

from helpdesk.application.eval_metrics import coverage_metrics
from helpdesk.application.evaluation import EvalCase, ExperimentLedger
from helpdesk.application.model_session import Budget, ModelSession, RateLimitedModelError
from helpdesk.domain.errors import AppError
from helpdesk.domain.models import Principal
from helpdesk.domain.rag import BoundScope, Chunk
from helpdesk.graph.baseline import Context, GraphRunner
from helpdesk.ports.chat import ChatRunInput


@pytest.mark.parametrize(
    "stage,node",
    [
        ("scope", "resolve_scope"),
        ("split", "plan_query"),
        ("embedding", "retrieve"),
        ("rerank", "rerank"),
        ("sufficiency", "sufficiency"),
        ("generate", "generate_stream"),
    ],
)
async def test_failure_preserves_only_completed_stage_observations(tmp_path, stage, node):
    chunks = [
        Chunk(
            "c" + str(i),
            "v" + str(i),
            "d" + str(i),
            "k" + str(i),
            "KB",
            "ORG",
            "title",
            "first evidence",
            {"paragraph": 1, "start": 0, "end": 14},
            "hash",
        )
        for i in (1, 2)
    ]

    class Corpus:
        async def chunks(self, principal, scope, kb):
            return [c for c in chunks if c.kb_id == kb]

        async def vector(self, principal, scope, kb, vector):
            return [c for c in chunks if c.kb_id == kb]

    provider = FakeModels()
    provider.requires_all = True
    original_structured, original_embed, original_rank = provider.structured, provider.embed, provider.rank

    async def structured(current, system, payload, limit):
        if current == stage:
            raise RateLimitedModelError(120, "seconds", "safe-request")
        return await original_structured(current, system, payload, limit)

    async def embed(texts):
        if stage == "embedding":
            raise RateLimitedModelError(120, "seconds")
        return await original_embed(texts)

    async def rank(query, texts):
        if stage == "rerank":
            raise RateLimitedModelError(120, "seconds")
        return await original_rank(query, texts)

    async def generate(system, payload, limit):
        raise RateLimitedModelError(120, "seconds")
        yield

    provider.structured, provider.embed, provider.rank = structured, embed, rank
    if stage == "generate":
        provider.generate = generate
    principal = Principal("u", "synthetic", "user", "demo")
    scope = BoundScope(
        ("k1", "k2"),
        tuple({"id": k, "name": "KB", "organization": "ORG"} for k in ("k1", "k2")),
        ("v1", "v2"),
        tuple({"id": "s" + k, "kb_id": k, "version_ids": ["v" + k[-1]]} for k in ("k1", "k2")),
        0,
        "scope",
    )
    ledger = ExperimentLedger(tmp_path / "ledger.db", 24, 200000)

    async def guard():
        pass

    models = ModelSession(provider, Budget(time.monotonic() + 110, 24, 200000), guard, ledger, "run")
    run = ChatRunInput(
        "r", "m", principal, 0, scope.kb_ids, "分别说明第一项和第二项", (), "split-v1", models.budget.deadline
    )
    cpu = ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn"))
    try:
        runner = GraphRunner(Context(run, scope, models, Corpus(), cpu, asyncio.Semaphore(1)))
        with pytest.raises(AppError):
            async for _ in runner.events():
                pass
        trace = runner.diagnostics()
        assert trace["node_observations"][node] == "failed" and not trace["trace_complete"]
        assert trace["node_observations"]["finalize"] == "not_reached"
        assert "first evidence" not in json.dumps(trace) and "分别说明" not in json.dumps(trace)
        if stage == "split":
            assert trace["split"] == {"attempted": True, "accepted": None}
        case = EvalCase(
            id="x",
            question="q",
            kb_ids=["k1", "k2"],
            expected_behavior="answer",
            theme="t",
            split="dev",
            reviewer="synthetic",
            reviewed_at="today",
            required_facts=["f"],
            evidence_groups=[
                [{"version_id": "v1", "paragraph": 1, "start": 0, "end": 14, "quote": "first evidence"}]
            ],
        )
        metrics = coverage_metrics(case, trace, [{**c.evidence("C1"), "id": c.id} for c in chunks])
        if stage in ("sufficiency", "generate"):
            assert metrics["context_coverage"]["full"] is True
            assert metrics["recall_at_5"]["recall"] == 1
        else:
            assert metrics["context_coverage"]["recall"] is None
        if stage == "rerank":
            assert metrics["fused_coverage"]["recall"] == 1
    finally:
        cpu.shutdown(wait=True, cancel_futures=True)
        ledger.close()


def test_observed_empty_candidates_are_zero_not_unobserved():
    case = EvalCase(
        id="x",
        question="q",
        kb_ids=["k"],
        expected_behavior="answer",
        theme="t",
        split="dev",
        reviewer="r",
        reviewed_at="today",
        required_facts=["f"],
        evidence_groups=[[{"version_id": "v", "paragraph": 1, "start": 0, "end": 1, "quote": "a"}]],
    )
    result = coverage_metrics(case, {"node_observations": {"retrieve": "observed"}, "candidate_ids": {}}, [])
    assert result["candidates_coverage"]["recall"] == 0
    assert result["fused_coverage"]["recall"] is None
