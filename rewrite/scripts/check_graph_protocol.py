"""推送图的冻结外部契约核对；历史 IT 协议可单独复核，不调用真实模型。"""

import asyncio
import argparse
import hashlib
import importlib
import io
import json
from multiprocessing import get_context
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor
from uuid import uuid4

import httpx

from protocol_fixture import handler, semantic_calls

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "rewrite/backend/tests/fixtures"
REFERENCE = "096a19b"


def inputs(filename="push-wire-input.json"):
    data = json.loads((FIXTURES / filename).read_text(encoding="utf-8"))
    return [
        {
            **item,
            "members": data["members"],
            "chunks": data["chunks"],
            "vector_order": sorted(c["id"] for c in data["chunks"]),
        }
        for item in data["cases"]
    ]


def legacy(fixtures):
    source = ROOT / "data/rewrite-m3-legacy" / REFERENCE
    archived = subprocess.check_output(
        ["git", "archive", "--format=zip", REFERENCE, "rag/helpdesk"]
    )
    with zipfile.ZipFile(io.BytesIO(archived)) as archive:
        for item in archive.infolist():
            target = (source / item.filename).resolve()
            if not target.is_relative_to(source.resolve()):
                raise ValueError("归档路径越界")
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            content = archive.read(item)
            if target.exists():
                if target.read_bytes() != content:
                    raise ValueError("旧源码测试副本发生漂移")
            else:
                target.write_bytes(content)
    env = {
        k: v
        for k, v in os.environ.items()
        if k in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    }
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [
            str(ROOT / "rag/.venv/Scripts/python.exe"),
            "-I",
            str(Path(__file__).with_name("legacy_graph_probe.py")),
            str(source),
        ],
        input=json.dumps(fixtures, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode:
        raise RuntimeError("旧协议隔离捕获失败：" + result.stderr[-2000:])
    rows = json.loads(result.stdout)
    for row in rows:
        assert str(source) in row.pop("source")
    return rows


async def current(fixtures):
    from helpdesk.application.evaluation import ExperimentLedger
    from helpdesk.application.model_session import Budget, ModelSession
    from helpdesk.adapters.models.gateway import HttpModels
    from helpdesk.domain.rag import BoundScope, Chunk
    from helpdesk.domain.models import Principal
    from helpdesk.graph.baseline import Context, GraphRunner
    from helpdesk.ports.chat import ChatRunInput
    from helpdesk.settings import Settings

    class Corpus:
        async def metadata(self, principal, scope):
            return scope

        async def check(self, *_):
            pass

        async def chunks(self, principal, scope, kb_id):
            return [c for c in self.rows if c.kb_id == kb_id]

        async def vector(self, principal, scope, kb_id, vector):
            return sorted(
                [c for c in self.rows if c.kb_id == kb_id], key=lambda c: c.id
            )[:20]

    cpu = ProcessPoolExecutor(max_workers=2, mp_context=get_context("spawn"))
    try:
        results = []
        for fixture in fixtures:
            members = [
                m
                for m in fixture["members"]
                if not fixture.get("kb_ids") or m["kb_id"] in fixture["kb_ids"]
            ]
            scope = BoundScope(
                tuple(m["kb_id"] for m in members),
                tuple(
                    {
                        "id": m["kb_id"],
                        "name": m["name"],
                        "organization": m["organization"],
                    }
                    for m in members
                ),
                tuple(v for m in members for v in m["version_ids"]),
                tuple(members),
                0,
                "synthetic",
            )
            corpus = Corpus()
            corpus.rows = [
                Chunk(
                    c["id"],
                    c["version_id"],
                    "doc-" + c["kb_id"],
                    c["kb_id"],
                    c["knowledge_base_name"],
                    c["organization"],
                    c["title"],
                    c["text"],
                    c["locator"],
                    c["content_hash"],
                )
                for c in fixture["chunks"]
                if c["kb_id"] in scope.kb_ids
            ]
            calls = []
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler(fixture, calls))
            ) as client:
                settings = Settings(
                    database_url="postgresql://unused",
                    upload_dir="unused",
                    chat_url="https://localhost/v1/chat/completions",
                    embedding_url="https://localhost/v1/embeddings",
                    rerank_url="https://localhost/v1/rerank",
                    chat_model="chat",
                    embedding_model="embedding",
                    rerank_model="rerank",
                    embedding_dimension=3,
                    rerank_format="generic",
                    token_reservation_limit=500000,
                )
                ledger = ExperimentLedger(Path(":memory:"), 100, 500000)

                async def guard():
                    pass

                session = ModelSession(
                    HttpModels(settings, client),
                    Budget(time.monotonic() + 30, 24, 500000),
                    guard,
                    ledger,
                    "test",
                )
                run = ChatRunInput(
                    "r",
                    "m",
                    Principal("user", "synthetic", "user", "demo"),
                    0,
                    scope.kb_ids,
                    fixture["question"],
                    tuple(fixture.get("history", [])),
                    fixture["strategy"],
                    session.budget.deadline,
                )
                graph = GraphRunner(
                    Context(run, scope, session, corpus, cpu, asyncio.Semaphore(2))
                )
                [event async for event in graph.events()]
                outcome = graph.outcome
                assert outcome
                results.append(
                    {
                        "id": fixture["id"],
                        "calls": semantic_calls(calls),
                        "selected_ids": outcome.trace.get("context_ids", []),
                        "fused_ids": outcome.trace.get("fused_ids", []),
                        "status": outcome.status,
                        "answer": outcome.answer,
                    }
                )
                ledger.close()
        return results
    finally:
        await asyncio.to_thread(cpu.shutdown, wait=True, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live-reference",
        action="store_true",
        help="额外用冻结旧解释器验证，仍为零模型网络调用",
    )
    args = parser.parse_args()
    fixtures = inputs()
    expected = json.loads(
        (FIXTURES / "push-wire-expected.json").read_text(encoding="utf-8")
    )
    source = json.loads(
        (FIXTURES / "push-wire-input.json").read_text(encoding="utf-8")
    )
    assert (
        expected["input_hash"]
        == hashlib.sha256(
            json.dumps(
                source, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )
    if args.live_reference:
        historical = json.loads((FIXTURES / "scope-wire-expected.json").read_text(encoding="utf-8"))
        assert legacy(inputs("scope-wire-input.json")) == historical["results"], "历史 IT 协议与冻结样本不同"
    old = expected["results"]
    with asyncio.Runner(
        loop_factory=asyncio.SelectorEventLoop
        if sys.platform == "win32"
        else asyncio.new_event_loop
    ) as runner:
        new = runner.run(current(fixtures))
    differences = []
    for left, right in zip(old, new, strict=True):
        for field in ["calls", "selected_ids", "fused_ids", "status", "answer"]:
            if left[field] != right[field]:
                differences.append({"id": left["id"], "field": field})
    folder = ROOT / "data" / ("rewrite-m3-protocol-" + uuid4().hex[:8])
    folder.mkdir(parents=True)
    (folder / "expected.json").write_text(
        json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (folder / "current.json").write_text(
        json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = {
        "contract": "push-domain-v1",
        "historical_reference_commit": REFERENCE,
        "historical_reference_checked": args.live_reference,
        "fixtures": len(fixtures),
        "differences": differences,
        "model_network_calls": 0,
        "output": str(folder.relative_to(ROOT)),
    }
    (folder / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    if differences:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
