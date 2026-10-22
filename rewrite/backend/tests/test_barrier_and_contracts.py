import asyncio
import json
from pathlib import Path

import pytest
from conftest import grant
from pydantic import ValidationError

from helpdesk.application.barrier import AuthorizationBarrier
from helpdesk.domain.contracts import AnswerCheck, ChatReceipt, CoverageReport


async def test_cancelled_writer_does_not_block_future_requests():
    barrier = AuthorizationBarrier()
    async with barrier.read():
        entered = asyncio.Event()

        async def writer():
            entered.set()
            async with barrier.write():
                pass

        task = asyncio.create_task(writer())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with asyncio.timeout(1):
        async with barrier.write():
            pass
        async with barrier.read():
            pass


async def test_policy_commit_cancels_registered_old_run(client, admin, app, database):
    services = app.state.services
    ready = asyncio.Event()

    async def run():
        ready.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await ready.wait()
    async with services.barrier.read(), services.uow() as repos:
        revision = await repos.access.revision()
        services.barrier.register("private-run", revision, task)
    response = await client.post("/api/v1/admin/groups", headers=admin, json={"name": "revoke-trigger"})
    assert response.status_code == 204
    with pytest.raises(asyncio.CancelledError):
        await task
    async with services.uow() as repos:
        assert await repos.access.revision() > revision
    services.barrier.unregister("private-run")


async def test_response_guard_blocks_metadata_computed_before_revoke(
    client, admin, user, document, app, monkeypatch
):
    await grant(client, admin, document)
    service = app.state.services.catalog
    original = service.documents
    computed, release = asyncio.Event(), asyncio.Event()

    async def slow(*args):
        rows = await original(*args)
        computed.set()
        await release.wait()
        return rows

    monkeypatch.setattr(service, "documents", slow)
    task = asyncio.create_task(client.get("/api/v1/documents?kb_id=" + document["kb_id"], headers=user[1]))
    await computed.wait()
    await grant(client, admin, document, "groups")
    release.set()
    response = await task
    assert response.status_code == 409 and response.json()["code"] == "authorization_changed"
    assert "配置文档" not in response.text and document["document_id"] not in response.text


def test_coverage_contract_rejects_forged_or_inconsistent_support():
    base = {
        "schema": "coverage-v1",
        "round": 0,
        "context_hash": "frozen",
        "missing_task_ids": [],
        "tasks": [
            {
                "task_id": "T1",
                "status": "covered",
                "support": [],
                "missing_requirements": [],
                "reason": "looks relevant",
            }
        ],
    }
    with pytest.raises(ValidationError):
        CoverageReport.model_validate(base)
    base["tasks"][0].update(status="missing", missing_requirements=["required action"])
    with pytest.raises(ValidationError):
        CoverageReport.model_validate(base)
    base["missing_task_ids"] = ["T1"]
    assert CoverageReport.model_validate(base).tasks[0].support == []


def test_answer_check_cannot_pass_empty_or_unsupported_claims():
    base = {
        "schema": "answer-check-v1",
        "draft_hash": "frozen",
        "context_hash": "evidence",
        "claims": [],
        "missing_task_ids": [],
        "unsupported_claim_ids": [],
        "invalid_citation_labels": [],
        "verdict": "pass",
    }
    with pytest.raises(ValidationError):
        AnswerCheck.model_validate(base)
    base["claims"] = [
        {
            "claim_id": "C1",
            "start": 0,
            "end": 8,
            "task_ids": ["T1"],
            "citation_labels": [],
            "evidence_spans": [],
            "status": "unsupported",
        }
    ]
    base["unsupported_claim_ids"] = ["C1"]
    with pytest.raises(ValidationError):
        AnswerCheck.model_validate(base)


def test_domain_imports_do_not_depend_on_framework_or_old_implementation():
    import ast

    root = Path(__file__).resolve().parents[1] / "src/helpdesk"
    forbidden = {"rag", "backend", "fastapi", "psycopg", "langgraph"}
    for file in (root / "domain").glob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in forbidden
    for file in root.rglob("*.py"):
        text = file.read_text(encoding="utf-8")
        assert "sys.path" not in text and "Pipeline.events" not in text and "from rag" not in text


def test_exported_schemas_match_models():
    folder = Path(__file__).resolve().parents[2] / "contracts"
    for filename, model in [
        ("coverage-v1.schema.json", CoverageReport),
        ("answer-check-v1.schema.json", AnswerCheck),
        ("chat-receipt-v1.schema.json", ChatReceipt),
    ]:
        assert json.loads((folder / filename).read_text(encoding="utf-8")) == model.model_json_schema(
            by_alias=True
        )
