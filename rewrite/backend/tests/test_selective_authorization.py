import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import psycopg
import pytest
from conftest import grant
from test_m2_pipeline import answer, publish, wait_run
from test_m2_pipeline import app as app
from test_m3_permissions import new_document

from helpdesk.application.authorization import DEPENDENCY_SCHEMA, TrackedCorpus
from helpdesk.domain.errors import AppError
from helpdesk.domain.rag import fingerprint
from helpdesk.entrypoints.evaluate import check_frozen_input, scope_identity


async def prepare(client, headers, app, kb_ids, key="prepared", sid=None):
    if sid is None:
        response = await client.post("/api/v1/sessions", headers=headers, json={"kb_ids": kb_ids})
        assert response.status_code == 201, response.text
        sid = response.json()["id"]
    token = headers["Authorization"].split()[1]
    svc = app.state.services
    principal = await svc.identity.authenticate(token)
    prepared = await svc.chat.prepare(principal, token, sid, key, "如何设置？")
    return sid, prepared, TrackedCorpus(prepared.authorization)


async def start_blocked(client, headers, app, document):
    provider = app.state.test_models
    provider.entered.clear()
    provider.wait_generate = asyncio.Event()
    sid = (
        await client.post("/api/v1/sessions", headers=headers, json={"kb_ids": [document["kb_id"]]})
    ).json()["id"]
    result = await client.post(
        "/api/v1/chat",
        headers=headers,
        json={
            "session_id": sid,
            "request_id": "blocked",
            "question": "如何设置？",
        },
    )
    assert result.status_code == 202, result.text
    await asyncio.wait_for(provider.entered.wait(), 15)
    return sid, result, app.state.services.chat._runs[result.json()["message_id"]]


@pytest.mark.parametrize("operation", ["group", "user", "other_password", "usage_kind"])
async def test_unrelated_changes_keep_generation(client, admin, user, document, app, operation):
    await publish(app)
    await grant(client, admin, document)
    sid, result, task = await start_blocked(client, user[1], app, document)
    if operation == "group":
        changed = await client.post("/api/v1/admin/groups", headers=admin, json={"name": "unrelated"})
    elif operation == "usage_kind":
        changed = await client.patch(
            "/api/v1/admin/users/" + user[0]["id"], headers=admin, json={"usage_kind": "real_usage"}
        )
    else:
        changed = await client.post(
            "/api/v1/admin/users", headers=admin, json={"username": "bob", "password": "AnotherUser123!"}
        )
        if operation == "other_password":
            rows = (await client.get("/api/v1/admin/users", headers=admin)).json()
            target = next(row["id"] for row in rows if row["username"] == "bob")
            changed = await client.patch(
                "/api/v1/admin/users/" + target, headers=admin, json={"password": "ChangedPassword123!"}
            )
    assert changed.status_code == 204, changed.text
    assert not task.done() and not task.cancelling()
    assert (await client.get(f"/api/v1/sessions/{sid}/messages", headers=user[1])).status_code == 200
    app.state.test_models.wait_generate.set()
    await wait_run(app, result)
    assert (await answer(client, user[1], sid, result))["status"] == "completed"


@pytest.mark.parametrize("operation", ["logout", "password", "disabled", "document_disabled"])
async def test_actual_identity_or_document_loss_cancels(
    client,
    admin,
    user,
    document,
    app,
    database,
    operation,
):
    await publish(app)
    await grant(client, admin, document)
    _, result, task = await start_blocked(client, user[1], app, document)
    if operation == "logout":
        response = await client.post("/api/v1/auth/logout", headers=user[1])
    elif operation == "document_disabled":
        response = await client.patch(
            f"/api/v1/documents/{document['document_id']}/enabled", headers=admin, json={"enabled": False}
        )
    else:
        response = await client.patch(
            "/api/v1/admin/users/" + user[0]["id"],
            headers=admin,
            json={"password": "ChangedPassword123!"} if operation == "password" else {"disabled": True},
        )
    assert response.status_code == 204, response.text
    await wait_run(app, result)
    assert task.done()
    with psycopg.connect(database) as conn:
        row = conn.execute(
            "SELECT status,answer,trace->>'stop_reason' FROM messages WHERE id=%s",
            (result.json()["message_id"],),
        ).fetchone()
    assert row == ("interrupted", "", "authorization_changed")


async def test_group_union_retains_access_until_last_grant_removed(client, admin, user, document, app):
    await publish(app)
    for name in ("first", "second"):
        assert (
            await client.post("/api/v1/admin/groups", headers=admin, json={"name": name})
        ).status_code == 204
    groups = [row["id"] for row in (await client.get("/api/v1/admin/groups", headers=admin)).json()]
    for group in groups:
        await client.put(
            f"/api/v1/admin/groups/{group}/members", headers=admin, json={"user_ids": [user[0]["id"]]}
        )
    await grant(client, admin, document, "groups", groups)
    _, result, task = await start_blocked(client, user[1], app, document)
    response = await client.put(
        f"/api/v1/admin/groups/{groups[0]}/members", headers=admin, json={"user_ids": []}
    )
    assert response.status_code == 204 and not task.cancelling()
    response = await client.put(
        f"/api/v1/admin/groups/{groups[1]}/members", headers=admin, json={"user_ids": []}
    )
    assert response.status_code == 204
    await wait_run(app, result)
    assert task.done()


async def test_unread_candidate_revocation_is_filtered_without_cancelling(
    client,
    admin,
    user,
    document,
    app,
):
    await publish(app)
    await grant(client, admin, document)
    _, prepared, tracked = await prepare(client, user[1], app, [document["kb_id"]])
    assert prepared.scope.version_ids == (document["version_id"],)
    await grant(client, admin, document, "groups")
    await prepared.authorization.guarded_check()
    visible = await tracked.metadata(prepared.run.principal, prepared.scope)
    assert visible.version_ids == () and visible.knowledge_bases == ()
    assert await tracked.chunks(prepared.run.principal, prepared.scope, document["kb_id"]) == []
    assert not prepared.authorization.dependencies.version_ids


async def test_added_permissions_do_not_expand_old_candidate_scope(client, admin, user, document, app):
    await publish(app)
    await grant(client, admin, document)
    hidden = await new_document(client, admin, app, "newly-authorized", document["kb_id"])
    _, prepared, tracked = await prepare(client, user[1], app, [document["kb_id"]])
    await grant(client, admin, hidden)
    chunks = await tracked.chunks(prepared.run.principal, prepared.scope, document["kb_id"])
    assert {chunk.version_id for chunk in chunks} == {document["version_id"]}
    assert hidden["version_id"] not in prepared.authorization.dependencies.version_ids
    _, fresh, _ = await prepare(client, user[1], app, [document["kb_id"]], key="fresh")
    assert set(fresh.scope.version_ids) == {document["version_id"], hidden["version_id"]}


async def test_non_cited_lexical_source_is_still_a_dependency(client, admin, user, document, app):
    await publish(app)
    await grant(client, admin, document)
    other = await new_document(client, admin, app, "not-finally-cited", document["kb_id"])
    await grant(client, admin, other)
    _, prepared, tracked = await prepare(client, user[1], app, [document["kb_id"]])
    chunks = await tracked.chunks(prepared.run.principal, prepared.scope, document["kb_id"])
    assert {chunk.version_id for chunk in chunks} == {document["version_id"], other["version_id"]}
    await grant(client, admin, other, "groups")
    with pytest.raises(AppError, match="资料访问权限"):
        await prepared.authorization.guarded_check()


async def test_metadata_only_dependency_checks_effective_kb_visibility(
    client,
    admin,
    user,
    document,
    app,
):
    await publish(app)
    await grant(client, admin, document)
    other = await new_document(client, admin, app, "another-readable", document["kb_id"])
    await grant(client, admin, other)
    _, prepared, tracked = await prepare(client, user[1], app, [document["kb_id"]])
    await tracked.metadata(prepared.run.principal, prepared.scope)
    assert prepared.authorization.dependencies.kb_ids == {document["kb_id"]}
    assert not prepared.authorization.dependencies.version_ids
    await grant(client, admin, document, "groups")
    await prepared.authorization.guarded_check()
    await grant(client, admin, other, "groups")
    with pytest.raises(AppError) as error:
        await prepared.authorization.guarded_check()
    assert error.value.code == "authorization_changed"


async def test_history_sources_are_transitively_inherited_and_checked(
    client,
    admin,
    user,
    document,
    app,
    database,
):
    await publish(app)
    await grant(client, admin, document)
    sid = (
        await client.post("/api/v1/sessions", headers=user[1], json={"kb_ids": [document["kb_id"]]})
    ).json()["id"]
    first = await client.post(
        "/api/v1/chat",
        headers=user[1],
        json={
            "session_id": sid,
            "request_id": "first",
            "question": "如何设置？",
        },
    )
    assert (await answer(client, user[1], sid, first))["status"] == "completed"
    _, second, _ = await prepare(client, user[1], app, [document["kb_id"]], key="second", sid=sid)
    assert document["version_id"] in second.authorization.dependencies.version_ids
    assert first.json()["message_id"] in second.authorization.dependencies.history_ids
    # 将带入的历史来源继续保存为第二轮的来源；第三轮必须继承这条间接依赖。
    await app.state.services.chat.store.finish(second.run.message_id, "completed", "第二轮回答", [], {})
    _, third, _ = await prepare(client, user[1], app, [document["kb_id"]], key="third", sid=sid)
    assert {first.json()["message_id"], second.run.message_id}.issubset(
        third.authorization.dependencies.history_ids
    )
    await grant(client, admin, document, "groups")
    with pytest.raises(AppError) as error:
        await third.authorization.guarded_check()
    assert error.value.code == "authorization_changed"
    with psycopg.connect(database) as conn:
        provenance = conn.execute(
            "SELECT provenance FROM messages WHERE id=%s", (third.run.message_id,)
        ).fetchone()[0]
    assert provenance["schema"] == DEPENDENCY_SCHEMA
    assert provenance["version_ids"] == [document["version_id"]]


async def test_only_selected_history_contributes_dependencies(client, admin, document, app, database):
    await publish(app)
    sid = (await client.post("/api/v1/sessions", headers=admin, json={"kb_ids": [document["kb_id"]]})).json()[
        "id"
    ]
    with psycopg.connect(database) as conn:
        for index in range(5):
            conn.execute(
                """INSERT INTO messages(id,session_id,request_id,question,answer,status,provenance,
                         created_at) VALUES(%s,%s,%s,'question','answer','completed',%s,
                         now() + (%s * interval '1 second'))""",
                (
                    f"history-{index}",
                    sid,
                    f"h-{index}",
                    psycopg.types.json.Jsonb(
                        {
                            "version_ids": [document["version_id"]],
                            "kb_ids": [document["kb_id"]],
                        }
                    ),
                    index,
                ),
            )
    _, prepared, _ = await prepare(client, admin, app, [document["kb_id"]], sid=sid)
    assert len(prepared.run.history) == 8
    assert prepared.authorization.dependencies.history_ids == {f"history-{i}" for i in range(1, 5)}


async def test_revoke_waits_for_read_and_dependency_registration(
    client,
    admin,
    user,
    document,
    app,
    monkeypatch,
):
    await publish(app)
    await grant(client, admin, document)
    _, prepared, tracked = await prepare(client, user[1], app, [document["kb_id"]])
    auth = prepared.authorization
    original = auth.store.save_provenance
    registering, release = asyncio.Event(), asyncio.Event()

    async def paused(*args):
        registering.set()
        await release.wait()
        await original(*args)

    monkeypatch.setattr(auth.store, "save_provenance", paused)
    reader = asyncio.create_task(tracked.chunks(prepared.run.principal, prepared.scope, document["kb_id"]))
    await asyncio.wait_for(registering.wait(), 5)
    writer = asyncio.create_task(grant(client, admin, document, "groups"))
    await asyncio.sleep(0.05)
    assert not writer.done()
    release.set()
    await reader
    await writer
    with pytest.raises(AppError) as error:
        await auth.guarded_check()
    assert error.value.code == "authorization_changed"


async def test_permission_check_failure_retries_then_fails_closed(
    client,
    admin,
    document,
    app,
    monkeypatch,
):
    await publish(app)
    _, prepared, tracked = await prepare(client, admin, app, [document["kb_id"]])
    calls = 0

    @asynccontextmanager
    async def unavailable():
        nonlocal calls
        calls += 1
        raise ConnectionError("private database details")
        yield

    monkeypatch.setattr(prepared.authorization, "uow", unavailable)
    app.state.test_models.calls.clear()
    with pytest.raises(AppError) as error:
        await tracked.chunks(prepared.run.principal, prepared.scope, document["kb_id"])
    assert error.value.code == "authorization_check_failed" and calls == 2
    assert "private" not in error.value.message
    assert app.state.test_models.calls == []
    with pytest.raises(AppError):
        await prepared.authorization.guarded_check()
    assert calls == 2


async def test_frozen_eval_ignores_unrelated_revision_but_rejects_changed_input(
    client,
    admin,
    user,
    document,
    app,
    monkeypatch,
):
    await publish(app)
    await grant(client, admin, document)
    _, prepared, _ = await prepare(client, user[1], app, [document["kb_id"]])
    corpus = app.state.services.chat.corpus
    principal, scope = prepared.run.principal, prepared.scope
    expected = fingerprint(await corpus.export(principal, scope))
    await client.post("/api/v1/admin/groups", headers=admin, json={"name": "eval-unrelated"})
    await check_frozen_input(corpus, principal, scope, expected)
    fresh = await corpus.bind(principal, scope.kb_ids, scope.revision)
    assert fresh.revision != scope.revision and fresh.scope_hash != scope.scope_hash
    assert scope_identity(fresh) == scope_identity(scope)
    original = corpus.export

    async def changed(*args):
        rows = await original(*args)
        return [{**row, "content_hash": "drift"} for row in rows]

    monkeypatch.setattr(corpus, "export", changed)
    with pytest.raises(AppError) as error:
        await check_frozen_input(corpus, principal, scope, expected)
    assert error.value.code == "evaluation_input_changed"


async def test_frozen_eval_additional_access_does_not_expand_input(client, admin, user, document, app):
    await publish(app)
    hidden = await new_document(client, admin, app, "not-in-frozen-input", document["kb_id"])
    await grant(client, admin, document)
    _, prepared, _ = await prepare(client, user[1], app, [document["kb_id"]])
    corpus = app.state.services.chat.corpus
    expected = fingerprint(await corpus.export(prepared.run.principal, prepared.scope))
    await grant(client, admin, hidden)
    await check_frozen_input(corpus, prepared.run.principal, prepared.scope, expected)
    assert prepared.scope.version_ids == (document["version_id"],)


def test_scope_identity_preserves_revision_as_audit_only():
    from helpdesk.domain.rag import BoundScope

    scope = BoundScope((), (), (), (), 28, "original")
    assert scope_identity(scope) == scope_identity(replace(scope, revision=29, scope_hash="new"))


@pytest.mark.parametrize("revoke", [False, True])
async def test_poll_refreshes_safely_across_authorization_change(
    client,
    admin,
    user,
    document,
    app,
    monkeypatch,
    revoke,
):
    await publish(app)
    await grant(client, admin, document)
    sid = (
        await client.post("/api/v1/sessions", headers=user[1], json={"kb_ids": [document["kb_id"]]})
    ).json()["id"]
    result = await client.post(
        "/api/v1/chat",
        headers=user[1],
        json={
            "session_id": sid,
            "request_id": "poll",
            "question": "设置？",
        },
    )
    assert (await answer(client, user[1], sid, result))["status"] == "completed"
    original = app.state.services.conversations.history
    computed, release = asyncio.Event(), asyncio.Event()

    async def paused(*args):
        rows = await original(*args)
        computed.set()
        await release.wait()
        return rows

    monkeypatch.setattr(app.state.services.conversations, "history", paused)
    polling = asyncio.create_task(client.get(f"/api/v1/sessions/{sid}/messages", headers=user[1]))
    await asyncio.wait_for(computed.wait(), 5)
    if revoke:
        await grant(client, admin, document, "groups")
    else:
        await client.post("/api/v1/admin/groups", headers=admin, json={"name": "poll-unrelated"})
    release.set()
    response = await polling
    assert response.status_code == 200, response.text
    row = response.json()[0]
    assert row["status"] == ("restricted" if revoke else "completed")
    assert row["answer"] == ("" if revoke else app.state.test_models.answer)


async def test_receipt_survives_unrelated_change_before_send(
    client,
    admin,
    user,
    document,
    app,
    monkeypatch,
):
    await publish(app)
    await grant(client, admin, document)
    provider = app.state.test_models
    provider.wait_generate = asyncio.Event()
    sid = (
        await client.post("/api/v1/sessions", headers=user[1], json={"kb_ids": [document["kb_id"]]})
    ).json()["id"]
    original = app.state.services.chat.submit
    accepted, release = asyncio.Event(), asyncio.Event()

    async def paused(*args):
        result = await original(*args)
        accepted.set()
        await release.wait()
        return result

    monkeypatch.setattr(app.state.services.chat, "submit", paused)
    pending = asyncio.create_task(
        client.post(
            "/api/v1/chat",
            headers=user[1],
            json={
                "session_id": sid,
                "request_id": "receipt",
                "question": "设置？",
            },
        )
    )
    await asyncio.wait_for(accepted.wait(), 5)
    await client.post("/api/v1/admin/groups", headers=admin, json={"name": "receipt-unrelated"})
    release.set()
    response = await pending
    assert response.status_code == 202, response.text
    provider.wait_generate.set()
    await wait_run(app, response)
    assert (await answer(client, user[1], sid, response))["status"] == "completed"


async def test_revoke_isolated_between_users(client, admin, user, document, app):
    await publish(app)
    await grant(client, admin, document)
    _, user_result, user_task = await start_blocked(client, user[1], app, document)
    admin_sid, admin_result, admin_task = await start_blocked(client, admin, app, document)
    await grant(client, admin, document, "groups")
    await wait_run(app, user_result)
    assert user_task.done() and not admin_task.done() and not admin_task.cancelling()
    app.state.test_models.wait_generate.set()
    await wait_run(app, admin_result)
    assert (await answer(client, admin, admin_sid, admin_result))["status"] == "completed"


async def test_revocation_check_failure_is_error_not_revoke(
    client,
    admin,
    user,
    document,
    app,
    database,
    monkeypatch,
):
    await publish(app)
    await grant(client, admin, document)
    _, result, _ = await start_blocked(client, user[1], app, document)

    async def unavailable(*args):
        raise ConnectionError("fixture database unavailable")

    monkeypatch.setattr(app.state.services.chat.corpus, "check", unavailable)
    response = await client.post("/api/v1/admin/groups", headers=admin, json={"name": "fault-trigger"})
    assert response.status_code == 204
    await wait_run(app, result)
    with psycopg.connect(database) as conn:
        row = conn.execute(
            "SELECT status,answer,trace->>'stop_reason' FROM messages WHERE id=%s",
            (result.json()["message_id"],),
        ).fetchone()
    assert row == ("error", "", "authorization_check_failed")


async def test_temporary_check_failure_can_retry_without_cancelling(
    client,
    admin,
    document,
    app,
    monkeypatch,
):
    await publish(app)
    _, prepared, tracked = await prepare(client, admin, app, [document["kb_id"]])
    auth, calls = prepared.authorization, 0
    original = auth.uow

    @asynccontextmanager
    async def intermittent():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("temporary")
        async with original() as repos:
            yield repos

    monkeypatch.setattr(auth, "uow", intermittent)
    assert await tracked.chunks(prepared.run.principal, prepared.scope, document["kb_id"])
    assert calls == 2 and auth.failure is None


async def test_admin_document_disable_still_cancels(client, admin, document, app):
    await publish(app)
    sid, result, _ = await start_blocked(client, admin, app, document)
    response = await client.patch(
        f"/api/v1/documents/{document['document_id']}/enabled", headers=admin, json={"enabled": False}
    )
    assert response.status_code == 204
    await wait_run(app, result)
    assert (await answer(client, admin, sid, result))["status"] == "restricted"


async def test_expired_credential_stops_at_runtime_check_without_policy_revision(
    client, admin, user, document, app, database,
):
    await publish(app)
    await grant(client, admin, document)
    _, result, _ = await start_blocked(client, user[1], app, document)
    with psycopg.connect(database) as conn:
        before = conn.execute("SELECT revision FROM authorization_state").fetchone()[0]
        conn.execute("UPDATE tokens SET expires_at=now()-interval '1 second' WHERE user_id=%s",
                     (user[0]["id"],))
    await wait_run(app, result)
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT revision FROM authorization_state").fetchone()[0] == before
        row = conn.execute("SELECT status,answer,trace->>'stop_reason' FROM messages WHERE id=%s",
                           (result.json()["message_id"],)).fetchone()
    assert row == ("interrupted", "", "authorization_changed")


async def test_frozen_evaluation_validation_after_unrelated_change(
    client,
    admin,
    user,
    document,
    app,
    tmp_path,
    monkeypatch,
):
    from helpdesk.entrypoints.evaluate import execute, freeze

    await publish(app)
    await grant(client, admin, document)
    config = app.state.test_settings
    cases = tmp_path / "cases.jsonl"
    cases.write_text(
        json.dumps(
            {
                "id": "frozen",
                "question": "缺少依据的问题",
                "kb_ids": [document["kb_id"]],
                "expected_behavior": "refuse",
                "theme": "fixture",
                "split": "dev",
                "reviewer": "fixture",
                "reviewed_at": "2026-10-25",
            }
        ),
        encoding="utf-8",
    )
    folder = tmp_path / "experiment"
    monkeypatch.setenv("HD_EVAL_TOKEN", user[1]["Authorization"].split()[1])
    await freeze(
        argparse.Namespace(
            cases=cases,
            out=folder,
            strategies="baseline",
            variants="D",
            seed=1,
            max_calls=10,
            reservation_limit=100000,
        ),
        config,
    )
    manifest_before = (folder / "manifest.json").read_bytes()
    await client.post("/api/v1/admin/groups", headers=admin, json={"name": "after-freeze"})
    await execute(argparse.Namespace(directory=folder, execute=False), config)
    assert (folder / "manifest.json").read_bytes() == manifest_before
    assert app.state.test_models.calls == [("embedding", ["private evidence"])]
    await grant(client, admin, document, "groups")
    with pytest.raises(AppError) as error:
        await execute(argparse.Namespace(directory=folder, execute=False), config)
    assert error.value.code == "authorization_changed"
    assert not (folder / "execution.lock").exists()
