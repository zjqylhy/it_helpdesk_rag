import asyncio
from contextlib import asynccontextmanager
from threading import Event

import psycopg
import pytest
from conftest import grant

from helpdesk.adapters.files.local import LocalFiles, complete_io


async def test_user_update_compatibility_and_password_audit(client, admin, user, database):
    password = "ChangedPassword123!"
    result = await client.patch(
        "/api/v1/admin/users/" + user[0]["id"],
        headers=admin,
        json={"password": password, "usage_kind": "real_usage"},
    )
    assert result.status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=user[1])).status_code == 401
    login = await client.post("/api/v1/auth/login", json={"username": "alice", "password": password})
    assert login.status_code == 200
    with psycopg.connect(database) as conn:
        details = conn.execute(
            "SELECT details::text FROM audit_events WHERE action='user.update'"
        ).fetchone()[0]
        assert "password_changed" in details and password not in details and "$2" not in details
        assert (
            conn.execute("SELECT usage_kind FROM users WHERE id=%s", (user[0]["id"],)).fetchone()[0]
            == "real_usage"
        )


async def test_default_search_uses_legacy_enabled_field(client, admin, document):
    path = "/api/v1/knowledge-bases/" + document["kb_id"] + "/default-search"
    assert (await client.patch(path, headers=admin, json={"enabled": False})).status_code == 204
    row = (await client.get("/api/v1/knowledge-bases", headers=admin)).json()[0]
    assert row["default_search"] is False
    assert (await client.patch(path, headers=admin, json={"default_search": False})).status_code == 400


async def test_cancelled_file_upload_removes_partial_files(tmp_path):
    store = LocalFiles(tmp_path / "files")
    waiting = asyncio.Event()

    async def parts():
        yield b"first block"
        waiting.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(store.save(parts(), ".txt"))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(store.root.iterdir()) == []


async def test_cancel_waits_for_inflight_file_write():
    entered, release = Event(), Event()

    def write():
        entered.set()
        assert release.wait(2)
        return 1

    task = asyncio.create_task(complete_io(write))
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_unknown_commit_confirmation_never_deletes_referenced_file(
    client, admin, document, app, monkeypatch, database
):
    service = app.state.services.catalog
    original = service.uow
    calls = 0

    @asynccontextmanager
    async def ambiguous():
        nonlocal calls
        calls += 1
        this_call = calls
        async with original() as repos:
            yield repos
        if this_call == 2:
            raise RuntimeError("commit_confirmation_lost")

    monkeypatch.setattr(service, "uow", ambiguous)
    principal = await app.state.services.identity.authenticate(admin["Authorization"].split()[1])

    async def parts():
        yield b"new version committed before lost confirmation"

    with pytest.raises(RuntimeError, match="commit_confirmation_lost"):
        await service.upload(principal, document["kb_id"], None, "new.txt", "new", "", False, parts())
    with psycopg.connect(database) as conn:
        keys = [row[0] for row in conn.execute("SELECT storage_key FROM document_versions")]
    assert len(keys) == 2 and all(service.files.path(key).exists() for key in keys)


async def test_revoked_session_title_does_not_leak_derived_metadata(
    client, admin, user, document, app, database
):
    await grant(client, admin, document)
    session = (
        await client.post("/api/v1/sessions", headers=user[1], json={"kb_ids": [document["kb_id"]]})
    ).json()
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE sessions SET title='自动生成的秘密标题' WHERE id=%s", (session["id"],))
    await grant(client, admin, document, "groups")
    row = (await client.get("/api/v1/sessions", headers=user[1])).json()[0]
    assert row["title"] == "受限会话" and row["kb_ids"] == [] and row["kb_id"] is None
