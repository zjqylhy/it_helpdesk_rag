import asyncio
import json

import psycopg
import pytest
from test_m2_pipeline import answer, publish, wait_run
from test_m2_pipeline import app as app

from helpdesk.domain.rag import TextDelta, Usage


async def session(client, headers, document):
    return (
        await client.post("/api/v1/sessions", headers=headers, json={"kb_ids": [document["kb_id"]]})
    ).json()["id"]


async def blocked_generation(app, monkeypatch):
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    provider = app.state.test_models

    async def generate(system, payload, limit):
        provider.calls.append(("generate", payload))
        try:
            yield TextDelta("已生成的草稿")
            entered.set()
            await release.wait()
            yield TextDelta(" [C1]。")
            yield TextDelta(finish_reason="stop", usage=Usage(5, 5))
        finally:
            closed.set()

    monkeypatch.setattr(provider, "generate", generate)
    return entered, release, closed


async def test_no_viewers_still_save_draft_and_finish(client, admin, document, app, database, monkeypatch):
    await publish(app)
    entered, release, closed = await blocked_generation(app, monkeypatch)
    sid = await session(client, admin, document)
    response = await client.post(
        "/api/v1/chat",
        headers=admin,
        json={
            "session_id": sid,
            "request_id": "unwatched",
            "question": "设置？",
        },
    )
    assert response.status_code == 202
    await asyncio.wait_for(entered.wait(), 20)
    # 不发任何进度请求，直接核对后台已自行落盘。
    async with asyncio.timeout(5):
        while True:
            with psycopg.connect(database) as conn:
                row = conn.execute("SELECT answer,status,citations FROM messages").fetchone()
            if row[0]:
                break
            await asyncio.sleep(0.02)
    assert row == ("已生成的草稿", "generating", [])
    release.set()
    await wait_run(app, response)
    message = await answer(client, admin, sid, response)
    assert message["answer"] == "已生成的草稿 [C1]。" and message["status"] == "completed"
    assert closed.is_set()
    assert len([k for k, _ in app.state.test_models.calls if k == "generate"]) == 1


async def test_concurrent_retries_execute_once_and_reject_conflicts(client, admin, document, app):
    await publish(app)
    provider = app.state.test_models
    provider.wait_generate = asyncio.Event()
    sid = await session(client, admin, document)
    body = {"session_id": sid, "request_id": "same", "question": "设置？"}
    responses = await asyncio.gather(
        *[client.post("/api/v1/chat", headers=admin, json=body) for _ in range(5)]
    )
    assert [r.status_code for r in responses].count(202) == 1
    assert [r.status_code for r in responses].count(200) == 4
    assert len({r.json()["message_id"] for r in responses}) == 1
    assert (
        await client.post("/api/v1/chat", headers=admin, json={**body, "question": "不同问题"})
    ).status_code == 409
    assert (
        await client.post("/api/v1/chat", headers=admin, json={**body, "request_id": "other"})
    ).status_code == 409
    provider.wait_generate.set()
    await wait_run(app, responses[0])
    assert len([k for k, _ in provider.calls if k == "generate"]) == 1


async def test_cancelled_submit_wait_does_not_cancel_admission(client, admin, document, app, monkeypatch):
    await publish(app)
    svc = app.state.services.chat
    saved, release = asyncio.Event(), asyncio.Event()
    original = svc.prepare

    async def paused(*args, **kwargs):
        prepared = await original(*args, **kwargs)
        saved.set()
        await release.wait()
        return prepared

    monkeypatch.setattr(svc, "prepare", paused)
    sid = await session(client, admin, document)
    body = {"session_id": sid, "request_id": "lost-ack", "question": "设置？"}
    request = asyncio.create_task(client.post("/api/v1/chat", headers=admin, json=body))
    await asyncio.wait_for(saved.wait(), 10)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    release.set()
    replay = await client.post("/api/v1/chat", headers=admin, json=body)
    assert replay.status_code == 200
    assert (await answer(client, admin, sid, replay))["status"] == "completed"
    assert len([k for k, _ in app.state.test_models.calls if k == "generate"]) == 1


async def test_broken_response_send_does_not_stop_execution(client, admin, document, app):
    await publish(app)
    sid = await session(client, admin, document)
    body = {"session_id": sid, "request_id": "broken-send", "question": "设置？"}
    encoded = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": encoded, "more_body": False}

    async def send(_):
        raise BrokenPipeError("browser disconnected")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/chat",
        "raw_path": b"/api/v1/chat",
        "query_string": b"",
        "headers": [
            (b"authorization", admin["Authorization"].encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(encoded)).encode()),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }
    with pytest.raises(BrokenPipeError):
        await app(scope, receive, send)
    replay = await client.post("/api/v1/chat", headers=admin, json=body)
    assert replay.status_code == 200
    assert (await answer(client, admin, sid, replay))["status"] == "completed"


async def test_explicit_stop_closes_generation_and_clears_draft(
    client, admin, user, document, app, monkeypatch
):
    await publish(app)
    entered, _, closed = await blocked_generation(app, monkeypatch)
    sid = await session(client, admin, document)
    response = await client.post(
        "/api/v1/chat",
        headers=admin,
        json={
            "session_id": sid,
            "request_id": "stop",
            "question": "设置？",
        },
    )
    mid = response.json()["message_id"]
    await asyncio.wait_for(entered.wait(), 20)
    path = f"/api/v1/sessions/{sid}/messages/{mid}/stop"
    assert (await client.post(path, headers=user[1])).status_code == 404
    stopped = await client.post(path, headers=admin)
    assert stopped.status_code == 200 and stopped.json()["status"] == "interrupted"
    assert closed.is_set()
    assert (await client.post(path, headers=admin)).json() == stopped.json()
    message = await answer(client, admin, sid, response)
    assert message["answer"] == "" and message["citations"] == []


async def test_late_abort_and_stop_cannot_overwrite_completed(client, admin, document, app):
    await publish(app)
    sid = await session(client, admin, document)
    response = await client.post(
        "/api/v1/chat",
        headers=admin,
        json={
            "session_id": sid,
            "request_id": "finished",
            "question": "设置？",
        },
    )
    message = await answer(client, admin, sid, response)
    mid = message["id"]
    await app.state.services.chat.store.abort(mid, "interrupted", "late_error")
    await app.state.services.chat.store.progress(mid, "过期草稿")
    stopped = await client.post(f"/api/v1/sessions/{sid}/messages/{mid}/stop", headers=admin)
    assert stopped.json()["status"] == "completed"
    assert await answer(client, admin, sid, response) == message


async def test_deadline_still_applies_without_viewers(client, admin, document, app, database):
    await publish(app)
    app.state.test_settings.request_timeout = 1
    app.state.test_models.wait_generate = asyncio.Event()
    sid = await session(client, admin, document)
    response = await client.post(
        "/api/v1/chat",
        headers=admin,
        json={
            "session_id": sid,
            "request_id": "deadline",
            "question": "设置？",
        },
    )
    await wait_run(app, response)
    with psycopg.connect(database) as conn:
        status, trace = conn.execute("SELECT status,trace FROM messages").fetchone()
    assert status == "error" and trace["stop_reason"] == "deadline"


async def test_shutdown_cancels_owned_tasks(client, admin, document, app, monkeypatch):
    await publish(app)
    entered, _, closed = await blocked_generation(app, monkeypatch)
    sid = await session(client, admin, document)
    response = await client.post(
        "/api/v1/chat",
        headers=admin,
        json={
            "session_id": sid,
            "request_id": "shutdown",
            "question": "设置？",
        },
    )
    await asyncio.wait_for(entered.wait(), 20)
    await app.state.services.chat.close()
    assert closed.is_set() and not app.state.services.chat._runs
    assert (await answer(client, admin, sid, response))["status"] == "interrupted"


async def test_failed_progress_save_cancels_upstream(client, admin, document, app, monkeypatch):
    await publish(app)
    entered, _, closed = await blocked_generation(app, monkeypatch)

    async def broken(*_):
        raise RuntimeError("synthetic storage failure")

    monkeypatch.setattr(app.state.services.chat.store, "progress", broken)
    sid = await session(client, admin, document)
    response = await client.post(
        "/api/v1/chat",
        headers=admin,
        json={
            "session_id": sid,
            "request_id": "storage-failure",
            "question": "设置？",
        },
    )
    await asyncio.wait_for(entered.wait(), 20)
    await wait_run(app, response)
    assert closed.is_set()
    message = await answer(client, admin, sid, response)
    assert message["status"] == "error" and message["answer"] == ""


async def test_failed_initialization_does_not_leave_unowned_message(
    client, admin, document, app, database, monkeypatch
):
    sid = await session(client, admin, document)

    def broken(*_):
        raise RuntimeError("synthetic runner failure")

    monkeypatch.setattr(app.state.services.chat, "runner", broken)
    with pytest.raises(RuntimeError, match="synthetic runner"):
        await client.post(
            "/api/v1/chat",
            headers=admin,
            json={
                "session_id": sid,
                "request_id": "initialization",
                "question": "设置？",
            },
        )
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT status FROM messages").fetchone()[0] == "error"
        assert conn.execute("SELECT status FROM model_runs").fetchone()[0] == "error"
    assert not app.state.services.chat._runs


async def test_recovery_clears_orphaned_draft_without_rerunning(client, admin, document, app, database):
    sid = await session(client, admin, document)
    svc = app.state.services
    token = admin["Authorization"][7:]
    principal = await svc.identity.authenticate(token)
    prepared = await svc.chat.prepare(principal, token, sid, "orphan", "设置？")
    await svc.chat.store.progress(prepared.run.message_id, "崩溃前的草稿")
    await svc.chat.store.recover()
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT status,answer,citations FROM messages").fetchone() == (
            "interrupted",
            "",
            [],
        )
    assert app.state.test_models.calls == []


async def test_repeated_revocation_does_not_interrupt_terminal_cleanup(
    client, admin, user, document, app, database, monkeypatch
):
    from conftest import grant

    await publish(app)
    await grant(client, admin, document)
    entered, _, _ = await blocked_generation(app, monkeypatch)
    cleanup, release = asyncio.Event(), asyncio.Event()
    original = app.state.services.chat.store.abort

    async def paused(*args):
        cleanup.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(app.state.services.chat.store, "abort", paused)
    sid = await session(client, user[1], document)
    response = await client.post(
        "/api/v1/chat",
        headers=user[1],
        json={
            "session_id": sid,
            "request_id": "repeated-revoke",
            "question": "设置？",
        },
    )
    await asyncio.wait_for(entered.wait(), 20)
    await grant(client, admin, document, "groups")
    await asyncio.wait_for(cleanup.wait(), 5)
    await grant(client, admin, document, "groups")
    release.set()
    await wait_run(app, response)
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT status,answer FROM messages").fetchone() == ("interrupted", "")


async def test_stop_during_admission_waits_for_background_handoff(client, admin, document, app, monkeypatch):
    await publish(app)
    svc = app.state.services.chat
    saved, release = asyncio.Event(), asyncio.Event()
    original = svc.prepare
    app.state.test_models.wait_generate = asyncio.Event()

    async def paused(*args, **kwargs):
        prepared = await original(*args, **kwargs)
        saved.set()
        await release.wait()
        return prepared

    monkeypatch.setattr(svc, "prepare", paused)
    sid = await session(client, admin, document)
    admission = asyncio.create_task(
        client.post(
            "/api/v1/chat",
            headers=admin,
            json={
                "session_id": sid,
                "request_id": "stop-admission",
                "question": "设置？",
            },
        )
    )
    await asyncio.wait_for(saved.wait(), 10)
    rows = (await client.get(f"/api/v1/sessions/{sid}/messages", headers=admin)).json()
    stopping = asyncio.create_task(
        client.post(
            f"/api/v1/sessions/{sid}/messages/{rows[0]['id']}/stop",
            headers=admin,
        )
    )
    await asyncio.sleep(0.02)
    assert not stopping.done()
    release.set()
    assert (await admission).status_code == 202
    assert (await stopping).json()["status"] == "interrupted"
