"""在隔离旧/新镜像中用相同合成语料生成协议指纹，不连接模型或数据库。"""

import argparse
import asyncio
import json
import time
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path

from helpdesk.application.evaluation import ExperimentLedger
from helpdesk.application.model_session import Budget, ModelSession
from helpdesk.domain.models import Principal
from helpdesk.domain.rag import BoundScope, Chunk, Reply, TextDelta, Usage, fingerprint
from helpdesk.graph.baseline import PROMPT_HASHES, Context, GraphRunner
from helpdesk.ports.chat import ChatRunInput


class Models:
    def __init__(self):
        self.calls = []

    async def structured(self, stage, system, payload, limit):
        self.calls.append([stage, system, payload, limit])
        if stage == "scope":
            value = {
                "query": payload["question"],
                "behavior": "search",
                "kb_ids": ["ka", "kb"],
                "requires_all": True,
                "option_ids": [],
                "message": "",
            }
        elif stage == "split":
            value = {"subqueries": ["第一项", "第二项"]}
        else:
            value = {"behavior": "answer", "reason": "fixture", "evidence_labels": ["C1", "C2"]}
        return Reply(value, Usage(10, 2))

    async def embed(self, texts):
        self.calls.append(["embedding", texts])
        return [[1.0, 0.2, 0.3] for _ in texts], Usage(5, 0)

    async def rank(self, query, texts):
        self.calls.append(["rerank", query, texts])
        return list(range(len(texts))), Usage(10, 0)

    async def generate(self, system, payload, limit):
        self.calls.append(["generate", system, payload, limit])
        yield TextDelta("甲校第一项 [C1]；乙校第二项 [C2]。")
        yield TextDelta(finish_reason="stop", usage=Usage(10, 8))


class Corpus:
    def __init__(self):
        self.rows = [
            Chunk(
                "c" + x,
                "v" + x,
                "d" + x,
                "k" + x,
                "KB" + x,
                "ORG" + x,
                "title",
                "synthetic evidence",
                {"paragraph": 1, "start": 0, "end": 18},
                "hash" + x,
            )
            for x in "ab"
        ]

    async def chunks(self, principal, scope, kb):
        return [c for c in self.rows if c.kb_id == kb]

    async def vector(self, principal, scope, kb, vector):
        return await self.chunks(principal, scope, kb)


async def main(out):
    principal = Principal("u", "synthetic", "user", "demo")
    scope = BoundScope(
        ("ka", "kb"),
        tuple({"id": "k" + x, "name": "KB" + x, "organization": "ORG" + x} for x in "ab"),
        ("va", "vb"),
        tuple({"id": "s" + x, "kb_id": "k" + x, "version_ids": ["v" + x]} for x in "ab"),
        0,
        "scope",
    )
    cpu = ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn"))
    result = {"prompt_hashes": PROMPT_HASHES, "arms": {}}
    try:
        for strategy in ("baseline", "split-v1"):
            provider = Models()
            ledger = ExperimentLedger(out.parent / (strategy + ".sqlite"), 24, 1000000)

            async def guard():
                return None

            session = ModelSession(
                provider, Budget(time.monotonic() + 110, 24, 1000000), guard, ledger, strategy
            )
            run = ChatRunInput(
                "r",
                "m",
                principal,
                0,
                scope.kb_ids,
                "比较第一项和第二项",
                (),
                strategy,
                session.budget.deadline,
            )
            runner = GraphRunner(Context(run, scope, session, Corpus(), cpu, asyncio.Semaphore(1)))
            events = []
            async for event in runner.events():
                events.append([event.event, event.data])
            assert runner.outcome is not None
            result["arms"][strategy] = {
                "model_calls": provider.calls,
                "events": events,
                "status": runner.outcome.status,
                "answer": runner.outcome.answer,
                "citations": runner.outcome.citations,
                "calls": session.started_calls,
            }
            ledger.close()
    finally:
        cpu.shutdown(wait=True, cancel_futures=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "protocol_hash": fingerprint(result),
                "calls": [result["arms"][s]["calls"] for s in result["arms"]],
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    asyncio.run(main(parser.parse_args().out))
