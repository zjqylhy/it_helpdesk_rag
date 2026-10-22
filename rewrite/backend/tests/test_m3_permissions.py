import asyncio
import json

import psycopg
from conftest import grant
from test_m2_pipeline import answer, publish, wait_run
from test_m2_pipeline import app as app


async def new_document(client, admin, app, text, kb_id=None, default=True):
    if kb_id is None:
        kb_id = (
            await client.post(
                "/api/v1/knowledge-bases", headers=admin, json={"name": text, "organization": text}
            )
        ).json()["id"]
    if not default:
        await client.patch(
            f"/api/v1/knowledge-bases/{kb_id}/default-search", headers=admin, json={"enabled": False}
        )
    doc = (
        await client.post(
            "/api/v1/documents",
            headers=admin,
            data={"kb_id": kb_id, "title": text},
            files={"file": ("synthetic.txt", text.encode())},
        )
    ).json()
    await publish(app)
    return {**doc, "kb_id": kb_id}


async def test_four_approved_scope_boundaries(client, admin, user, document, app):
    await publish(app)
    await grant(client, admin, document)
    hidden = await new_document(client, admin, app, "HIDDEN_SAME_KB", document["kb_id"])
    other = await new_document(client, admin, app, "HIDDEN_ORGANIZATION")
    excluded = await new_document(client, admin, app, "EXCLUDED_DEFAULT", default=False)
    await grant(client, admin, excluded)
    bases = (await client.get("/api/v1/knowledge-bases", headers=user[1])).json()
    assert {b["id"] for b in bases} == {document["kb_id"], excluded["kb_id"]}
    # 显式无权和不存在的范围/文件保持一致，不能用于枚举元数据。
    denied = await client.post("/api/v1/sessions", headers=user[1], json={"kb_ids": [other["kb_id"]]})
    missing = await client.post("/api/v1/sessions", headers=user[1], json={"kb_ids": ["missing-id"]})
    assert denied.status_code == missing.status_code == 400 and denied.json() == missing.json()
    denied_file = await client.get("/api/v1/files/" + hidden["version_id"], headers=user[1])
    missing_file = await client.get("/api/v1/files/missing-id", headers=user[1])
    assert (
        denied_file.status_code == missing_file.status_code == 404
        and denied_file.json() == missing_file.json()
    )
    session = (await client.post("/api/v1/sessions", headers=user[1], json={})).json()["id"]
    app.state.test_models.calls.clear()
    result = await client.post(
        "/api/v1/chat",
        headers=user[1],
        json={"session_id": session, "request_id": "boundaries", "question": "如何操作？"},
    )
    assert (await answer(client, user[1], session, result))["status"] == "completed"
    calls = json.dumps(app.state.test_models.calls, ensure_ascii=False)
    assert "HIDDEN" not in calls and "EXCLUDED_DEFAULT" not in calls
    citations = (await answer(client, user[1], session, result))["citations"]
    assert {c["version_id"] for c in citations} == {document["version_id"]}


async def test_group_revocation_hides_history_and_cancels_inflight(
    client, admin, user, document, app, database
):
    await publish(app)
    await client.post("/api/v1/admin/groups", headers=admin, json={"name": "synthetic-group"})
    group = (await client.get("/api/v1/admin/groups", headers=admin)).json()[0]["id"]

    async def membership(ids):
        response = await client.put(
            f"/api/v1/admin/groups/{group}/members", headers=admin, json={"user_ids": ids}
        )
        assert response.status_code == 204

    await membership([user[0]["id"]])
    await grant(client, admin, document, "groups", [group])
    session = (
        await client.post("/api/v1/sessions", headers=user[1], json={"kb_ids": [document["kb_id"]]})
    ).json()["id"]
    result = await client.post(
        "/api/v1/chat",
        headers=user[1],
        json={"session_id": session, "request_id": "before", "question": "如何操作？"},
    )
    assert (await answer(client, user[1], session, result))["status"] == "completed"
    await membership([])
    history = (await client.get(f"/api/v1/sessions/{session}/messages", headers=user[1])).json()
    assert (
        history[0]["status"] == "restricted"
        and history[0]["answer"] == ""
        and history[0]["citations"] == []
        and history[0]["trace"] == {}
    )
    app.state.test_models.calls.clear()
    denied = await client.post(
        "/api/v1/chat",
        headers=user[1],
        json={"session_id": session, "request_id": "after", "question": "继续说明"},
    )
    assert denied.status_code == 400 and app.state.test_models.calls == []
    await membership([user[0]["id"]])
    provider = app.state.test_models
    provider.entered.clear()
    provider.wait_generate = asyncio.Event()
    task = asyncio.create_task(
        client.post(
            "/api/v1/chat",
            headers=user[1],
            json={"session_id": session, "request_id": "inflight", "question": "慢速说明"},
        )
    )
    await asyncio.wait_for(provider.entered.wait(), 15)
    await membership([])
    result = await asyncio.wait_for(task, 10)
    await wait_run(app, result)
    assert (await answer(client, user[1], session, result))["status"] == "restricted"
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM messages WHERE status='generating'").fetchone()[0] == 0
