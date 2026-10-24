import asyncio
import json

import psycopg
import pytest
import pytest_asyncio
from conftest import grant
from fakes import FakeModels

from helpdesk.adapters.http.app import create_app
from helpdesk.adapters.parsing.paragraphs import parse
from helpdesk.adapters.parsing.process import parse_file
from helpdesk.adapters.postgres.chat import ChatStore
from helpdesk.adapters.postgres.jobs import JobStore
from helpdesk.application.ingestion import IngestionWorker
from helpdesk.domain.errors import AppError
from helpdesk.domain.rag import Usage
from helpdesk.settings import Settings


@pytest_asyncio.fixture
async def app(database, tmp_path):
    provider = FakeModels()
    settings = Settings(
        database_url=database,
        upload_dir=tmp_path / "files",
        embedding_model="fixture-embed",
        embedding_dimension=3,
        chat_model="fixture-chat",
        rerank_model="fixture-rank",
    )
    instance = create_app(settings, models=provider)
    async with instance.router.lifespan_context(instance):
        await instance.state.services.identity.create(
            None, "admin", "AdminTest123!", role="admin", bootstrap=True
        )
        instance.state.test_models = provider
        instance.state.test_settings = settings
        yield instance


async def publish(app):
    svc = app.state.services
    worker = IngestionWorker(
        JobStore(svc.uow.pool),
        svc.catalog.files,
        app.state.test_models,
        svc.chat.ledger,
        app.state.test_settings,
        parse_file,
    )
    assert await worker.once()


async def wait_run(app, response):
    assert response.status_code in (200, 202), response.text
    task = app.state.services.chat._runs.get(response.json()["message_id"])
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), 20)


async def answer(client, headers, session, response):
    assert response.status_code in (200, 202), response.text
    message_id = response.json()["message_id"]
    async with asyncio.timeout(20):
        while True:
            history = await client.get(f"/api/v1/sessions/{session}/messages", headers=headers)
            assert history.status_code == 200, history.text
            message = next(m for m in history.json() if m["id"] == message_id)
            if message["status"] != "generating":
                return message
            await asyncio.sleep(0.02)


async def test_real_graph_worker_background_and_persistence(client, admin, user, document, app, database):
    await publish(app)
    await grant(client, admin, document)
    session = (
        await client.post("/api/v1/sessions", headers=user[1], json={"kb_ids": [document["kb_id"]]})
    ).json()["id"]
    body = {"session_id": session, "request_id": "r1", "question": "如何打开设置？"}
    response = await client.post("/api/v1/chat", headers=user[1], json=body)
    assert response.status_code == 202
    message = await answer(client, user[1], session, response)
    assert message["status"] == "completed"
    assert message["answer"] == app.state.test_models.answer
    assert message["citations"][0]["version_id"] == document["version_id"]
    assert message["trace"] == {}
    calls = list(app.state.test_models.calls)
    replay = await client.post("/api/v1/chat", headers=user[1], json=body)
    assert replay.status_code == 200 and replay.json()["message_id"] == message["id"]
    assert app.state.test_models.calls == calls
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM model_runs WHERE message_id IS NOT NULL").fetchone()[0] == 1


async def test_invalid_citation_ends_error_not_completed(client, admin, document, app, database):
    await publish(app)
    app.state.test_models.answer = "错误编号 [C9]"
    session = (
        await client.post("/api/v1/sessions", headers=admin, json={"kb_ids": [document["kb_id"]]})
    ).json()["id"]
    response = await client.post(
        "/api/v1/chat", headers=admin, json={"session_id": session, "request_id": "bad", "question": "设置？"}
    )
    await wait_run(app, response)
    message = await answer(client, admin, session, response)
    assert message["status"] == "error" and message["answer"] == "" and message["citations"] == []
    with psycopg.connect(database) as conn:
        result = conn.execute(
            "SELECT manifest->'result' FROM model_runs WHERE message_id IS NOT NULL"
        ).fetchone()[0]
        assert result["stop_reason"] == "invalid_citations"
        assert result["node_observations"]["select_context"] == "observed"
        assert result["node_observations"]["validate_citations"] == "failed"
        assert result["attempt_details"] and "private evidence" not in json.dumps(result)
    assert "attempt_details" not in json.dumps(message) and "provider_request_id" not in json.dumps(message)


async def test_permission_change_cancels_background_without_polling(
    client, admin, user, document, app, database
):
    await publish(app)
    await grant(client, admin, document)
    provider = app.state.test_models
    provider.wait_generate = asyncio.Event()
    session = (
        await client.post("/api/v1/sessions", headers=user[1], json={"kb_ids": [document["kb_id"]]})
    ).json()["id"]
    response = await client.post(
        "/api/v1/chat",
        headers=user[1],
        json={"session_id": session, "request_id": "cancel", "question": "设置？"},
    )
    assert response.status_code == 202
    await asyncio.wait_for(provider.entered.wait(), 20)
    await grant(client, admin, document, "groups")
    await wait_run(app, response)
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT status,answer FROM messages").fetchone() == ("interrupted", "")
    history = (await client.get(f"/api/v1/sessions/{session}/messages", headers=user[1])).json()
    assert history[0]["status"] == "restricted" and history[0]["answer"] == ""


async def test_unauthorized_source_never_reaches_models(client, admin, user, document, app):
    await publish(app)
    app.state.test_models.calls.clear()
    session = (await client.post("/api/v1/sessions", headers=user[1], json={})).json()["id"]
    response = await client.post(
        "/api/v1/chat",
        headers=user[1],
        json={"session_id": session, "request_id": "empty", "question": "内容？"},
    )
    assert (await answer(client, user[1], session, response))["status"] == "refused"
    assert app.state.test_models.calls == []


async def test_lease_fencing_and_newest_version_wins(client, admin, document, app, database):
    jobs = JobStore(app.state.services.uow.pool)
    first = await jobs.claim()
    assert first is not None
    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE ingestion_jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (first.job_id,)
        )
    replacement = await jobs.claim()
    assert replacement and replacement.attempt == 2 and replacement.owner != first.owner
    chunks = parse(b"old version", "old.txt")
    with pytest.raises(AppError, match="租约"):
        await jobs.publish(first, chunks, [[1.0, 0.2, 0.3]], "fixture-embed", 3)
    new = (
        await client.post(
            f"/api/v1/documents/{document['document_id']}/versions",
            headers=admin,
            files={"file": ("new.txt", b"new version")},
        )
    ).json()
    latest = await jobs.claim()
    assert latest and latest.version_id == new["version_id"]
    assert await jobs.claim() is None
    await jobs.publish(latest, parse(b"new version", "new.txt"), [[1.0, 0.2, 0.3]], "fixture-embed", 3)
    await jobs.publish(replacement, chunks, [[1.0, 0.2, 0.3]], "fixture-embed", 3)
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT active_version FROM documents").fetchone()[0] == latest.version_id
        assert conn.execute("SELECT count(*) FROM ingestion_jobs WHERE status='completed'").fetchone()[0] == 2
    await client.patch(
        f"/api/v1/documents/{document['document_id']}/enabled", headers=admin, json={"enabled": False}
    )
    with psycopg.connect(database) as conn:
        assert (
            conn.execute("SELECT version_ids FROM snapshots ORDER BY revision DESC LIMIT 1").fetchone()[0]
            == []
        )


async def test_failed_new_version_preserves_previous_publication(client, admin, document, app, database):
    await publish(app)
    new = await client.post(
        f"/api/v1/documents/{document['document_id']}/versions",
        headers=admin,
        files={"file": ("invalid.txt", b"   ")},
    )
    assert new.status_code == 202
    await publish(app)
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT active_version FROM documents").fetchone()[0] == document["version_id"]
        assert (
            conn.execute(
                "SELECT status FROM document_versions WHERE id=%s", (new.json()["version_id"],)
            ).fetchone()[0]
            == "failed"
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM chunks WHERE version_id=%s", (new.json()["version_id"],)
            ).fetchone()[0]
            == 0
        )


async def test_publication_vector_validation_is_atomic(document, app, database):
    jobs = JobStore(app.state.services.uow.pool)
    lease = await jobs.claim()
    assert lease
    with pytest.raises(AppError):
        await jobs.publish(lease, parse(b"text", "test.txt"), [[1.0, 2.0]], "fixture-embed", 3)
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT active_version FROM documents").fetchone()[0] is None
        assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM ingestion_jobs").fetchone()[0] == "running"


async def test_api_recovery_does_not_own_worker_attempts(document, app, database):
    ledger = app.state.services.chat.ledger
    run_id = await ledger.start_run(None, {"kind": "ingestion"})
    attempt = await ledger.start_attempt(run_id, "embedding", 10)
    await ChatStore(app.state.services.uow.pool).recover()
    with psycopg.connect(database) as conn:
        assert (
            conn.execute("SELECT status FROM model_attempts WHERE id=%s", (attempt,)).fetchone()[0]
            == "started"
        )
        assert conn.execute("SELECT status FROM model_runs WHERE id=%s", (run_id,)).fetchone()[0] == "running"
    await ledger.end_attempt(attempt, "completed", Usage(10, 0))


async def test_eval_freeze_dryrun_same_graph_pair_and_drift(
    client, admin, document, app, tmp_path, monkeypatch
):
    from argparse import Namespace

    from helpdesk.application.evaluation import ExperimentLedger
    from helpdesk.entrypoints import evaluate

    await publish(app)
    monkeypatch.setenv("HD_EVAL_TOKEN", admin["Authorization"][7:])
    cases = tmp_path / "cases.jsonl"
    folder = tmp_path / "experiment"
    cases.write_text(
        json.dumps(
            {
                "id": "case1",
                "question": "分别说明第一项和第二项如何配置？",
                "kb_ids": [document["kb_id"]],
                "expected_behavior": "answer",
                "theme": "settings",
                "split": "dev",
                "reviewer": "synthetic-test",
                "reviewed_at": "2026-10-17",
                "required_facts": ["private evidence"],
                "evidence_groups": [
                    [
                        {
                            "version_id": document["version_id"],
                            "paragraph": 1,
                            "start": 0,
                            "end": 16,
                            "quote": "private evidence",
                        }
                    ]
                ],
            }
        ),
        encoding="utf-8",
    )
    args = Namespace(
        cases=cases,
        out=folder,
        strategies="baseline,split-v1",
        variants="D",
        seed=7,
        max_calls=50,
        reservation_limit=200000,
    )
    from fakes import rate_profile

    args.rate_profile = tmp_path / "rates.json"
    args.rate_profile.write_text(json.dumps(rate_profile(app.state.test_settings)), encoding="utf-8")
    await evaluate.freeze(args, app.state.test_settings)
    monkeypatch.setattr(evaluate.Settings, "models_ready", property(lambda self: True))
    provider = FakeModels()
    monkeypatch.setattr(evaluate, "HttpModels", lambda *_: provider)
    await evaluate.execute(Namespace(directory=folder, execute=False), app.state.test_settings)
    assert not provider.calls
    await evaluate.execute(Namespace(directory=folder, execute=True), app.state.test_settings)
    ledger = ExperimentLedger(folder / "ledger.sqlite", 50, 200000)
    rows = ledger.results()
    calls = ledger.summary()["calls"]
    ledger.close()
    assert len(rows) == 2 and all(r["status"] == "completed" for r in rows)
    assert all(r["context_coverage"]["full"] for r in rows)
    b0 = next(r for r in rows if r["strategy"] == "baseline")
    b1 = next(r for r in rows if r["strategy"] == "split-v1")
    assert b1["calls"] == b0["calls"] + 3 and b1["trace"]["split"]["accepted"]
    await evaluate.execute(Namespace(directory=folder, execute=True), app.state.test_settings)
    ledger = ExperimentLedger(folder / "ledger.sqlite", 50, 200000)
    assert ledger.summary()["calls"] == calls
    ledger.close()
    evaluate.report(Namespace(directory=folder, out=tmp_path / "report"))
    missing = Namespace(**{**vars(args), "out": tmp_path / "without-rates", "rate_profile": None})
    await evaluate.freeze(missing, app.state.test_settings)
    with pytest.raises(ValueError, match="rate-profile"):
        await evaluate.execute(Namespace(directory=missing.out, execute=True), app.state.test_settings)
    assert len(provider.calls) == calls
    state = folder / "scheduling.sqlite"
    saved_state = state.read_bytes()
    state.unlink()
    with pytest.raises(ValueError, match="运行状态缺失"):
        await evaluate.execute(Namespace(directory=folder, execute=True), app.state.test_settings)
    assert not state.exists()
    state.write_bytes(saved_state)
    original_profile = (folder / "rate-profile.json").read_bytes()
    profile_data = json.loads(original_profile)
    profile_data["routes"]["chat"]["rpm"] -= 1
    (folder / "rate-profile.json").write_text(json.dumps(profile_data), encoding="utf-8")
    with pytest.raises(ValueError, match="调度配置漂移"):
        await evaluate.execute(Namespace(directory=folder, execute=True), app.state.test_settings)
    (folder / "rate-profile.json").write_bytes(original_profile)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    manifest["code_hash"] = "drift"
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="漂移"):
        await evaluate.execute(Namespace(directory=folder, execute=True), app.state.test_settings)


async def test_scope_requires_all_over_budget_clarifies_without_retrieval(client, admin, app):
    for i in range(6):
        response = await client.post(
            "/api/v1/knowledge-bases", headers=admin, json={"name": f"库{i}", "organization": f"学校{i}"}
        )
        assert response.status_code == 201
        await client.post(
            "/api/v1/documents",
            headers=admin,
            data={"kb_id": response.json()["id"], "title": "合成文档"},
            files={"file": ("fixture.txt", f"合成内容{i}".encode())},
        )
        await publish(app)
    app.state.test_models.requires_all = True
    app.state.test_models.calls.clear()
    session = (await client.post("/api/v1/sessions", headers=admin, json={})).json()["id"]
    result = await client.post(
        "/api/v1/chat",
        headers=admin,
        json={"session_id": session, "request_id": "many", "question": "比较这些学校"},
    )
    message = await answer(client, admin, session, result)
    assert message["status"] == "clarify"
    assert "最多 5 个主题" in message["answer"] and "库0" in message["answer"]
    assert "学校" not in message["answer"] and "发生时间" not in message["answer"]
    assert [stage for stage, _ in app.state.test_models.calls] == ["scope"]


async def test_cross_organization_comparison_needs_each_objects_citations(client, admin, document, app):
    await publish(app)
    kb = (
        await client.post(
            "/api/v1/knowledge-bases", headers=admin, json={"name": "第二库", "organization": "第二学校"}
        )
    ).json()
    await client.post(
        "/api/v1/documents",
        headers=admin,
        data={"kb_id": kb["id"], "title": "第二操作"},
        files={"file": ("other.txt", b"other instructions")},
    )
    await publish(app)
    app.state.test_models.requires_all = True
    session = (await client.post("/api/v1/sessions", headers=admin, json={})).json()["id"]
    result = await client.post(
        "/api/v1/chat",
        headers=admin,
        json={"session_id": session, "request_id": "compare", "question": "比较两所学校的操作"},
    )
    assert (await answer(client, admin, session, result))["status"] == "error"


async def test_unknown_scope_id_never_becomes_retrieval_input(client, admin, document, app):
    await publish(app)
    await client.post(
        "/api/v1/knowledge-bases", headers=admin, json={"name": "第二库", "organization": "第二学校"}
    )
    provider = app.state.test_models
    provider.calls.clear()
    original = provider.structured

    async def invalid_scope(stage, system, payload, limit):
        result = await original(stage, system, payload, limit)
        if stage == "scope":
            result.value["kb_ids"] = ["not-authorized"]
        return result

    provider.structured = invalid_scope
    session = (await client.post("/api/v1/sessions", headers=admin, json={})).json()["id"]
    result = await client.post(
        "/api/v1/chat",
        headers=admin,
        json={"session_id": session, "request_id": "invalid", "question": "查询"},
    )
    assert (await answer(client, admin, session, result))["status"] == "error"
    assert [stage for stage, _ in provider.calls] == ["scope"]


async def test_request_budget_stops_before_more_http(client, admin, document, app):
    await publish(app)
    app.state.test_settings.request_calls = 1
    app.state.test_models.calls.clear()
    session = (
        await client.post("/api/v1/sessions", headers=admin, json={"kb_ids": [document["kb_id"]]})
    ).json()["id"]
    response = await client.post(
        "/api/v1/chat",
        headers=admin,
        json={"session_id": session, "request_id": "one-call", "question": "操作方法"},
    )
    assert (await answer(client, admin, session, response))["status"] == "error"
    assert [stage for stage, _ in app.state.test_models.calls] == ["embedding"]


@pytest.mark.parametrize(
    "variant,expected",
    [
        ("A", ["sufficiency", "generate"]),
        ("B", ["embedding", "sufficiency", "generate"]),
        ("C", ["embedding", "sufficiency", "generate"]),
        ("D", ["embedding", "rerank", "sufficiency", "generate"]),
    ],
)
async def test_single_kb_abcd_use_same_graph_and_frozen_generation(
    client, admin, document, app, variant, expected
):
    from dataclasses import replace

    from helpdesk.graph.baseline import GraphRunner

    await publish(app)
    svc = app.state.services
    token = admin["Authorization"][7:]
    principal = await svc.identity.authenticate(token)
    session = (
        await client.post("/api/v1/sessions", headers=admin, json={"kb_ids": [document["kb_id"]]})
    ).json()["id"]
    prepared = await svc.chat.prepare(principal, token, session, "variant", "配置方法")
    app.state.test_models.calls.clear()
    runner = GraphRunner(replace(svc.chat.runner(prepared).context, retrieval_variant=variant))
    [event async for event in runner.events()]
    assert runner.outcome and runner.outcome.status == "completed"
    assert [stage for stage, _ in app.state.test_models.calls] == expected
