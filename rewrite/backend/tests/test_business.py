import asyncio
import hashlib

import psycopg
import pytest
from conftest import grant

from helpdesk.domain.errors import AppError
from helpdesk.domain.models import MessageStatus


async def test_authentication_and_logout(client, admin, user, database):
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    assert (
        await client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"})
    ).status_code == 401
    assert (await client.get("/api/v1/auth/me", headers=user[1])).json()["ID"] == user[0]["id"]
    with psycopg.connect(database) as conn:
        token = user[1]["Authorization"].split()[1]
        assert conn.execute("SELECT count(*) FROM tokens WHERE token_hash=%s", (token,)).fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT count(*) FROM tokens WHERE token_hash=%s",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()[0]
            == 1
        )
    assert (await client.post("/api/v1/auth/logout", headers=user[1])).status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=user[1])).status_code == 401


async def test_expired_and_disabled_tokens(client, admin, user, database):
    result = await client.patch(
        "/api/v1/admin/users/" + user[0]["id"] + "/disabled", headers=admin, json={"disabled": True}
    )
    assert result.status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=user[1])).status_code == 401
    assert (
        await client.post("/api/v1/auth/login", json={"username": "alice", "password": "UserTest123!"})
    ).status_code == 401
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE tokens SET expires_at=now()-interval '1 minute'")
    assert (await client.get("/api/v1/auth/me", headers=admin)).status_code == 401


async def test_admin_boundary_and_last_admin(client, admin, user):
    assert (await client.get("/api/v1/admin/users", headers=user[1])).status_code == 403
    assert (
        await client.post("/api/v1/knowledge-bases", headers=user[1], json={"name": "x", "organization": "y"})
    ).status_code == 403
    me = (await client.get("/api/v1/auth/me", headers=admin)).json()
    assert (
        await client.patch(
            "/api/v1/admin/users/" + me["ID"] + "/disabled", headers=admin, json={"disabled": True}
        )
    ).status_code == 409
    assert (await client.get("/api/v1/auth/me", headers=admin)).status_code == 200


async def test_private_document_metadata_and_file_are_hidden(client, admin, user, document):
    assert (await client.get("/api/v1/knowledge-bases", headers=user[1])).json() == []
    assert (await client.get("/api/v1/documents?kb_id=" + document["kb_id"], headers=user[1])).json() == []
    private = await client.get("/api/v1/documents/" + document["document_id"] + "/versions", headers=user[1])
    absent = await client.get("/api/v1/documents/absent/versions", headers=user[1])
    assert private.status_code == absent.status_code == 404
    assert private.json() == absent.json()
    assert (await client.get("/api/v1/files/" + document["version_id"], headers=user[1])).status_code == 404
    await grant(client, admin, document)
    assert len((await client.get("/api/v1/knowledge-bases", headers=user[1])).json()) == 1
    versions = (
        await client.get("/api/v1/documents/" + document["document_id"] + "/versions", headers=user[1])
    ).json()
    assert len(versions) == 1 and "path" not in versions[0] and "storage_key" not in versions[0]
    assert (
        await client.get("/api/v1/files/" + document["version_id"], headers=user[1])
    ).content == b"private evidence"


async def test_group_union_and_revocation(client, admin, user, document):
    await client.post("/api/v1/admin/groups", headers=admin, json={"name": "第一组"})
    g1 = (await client.get("/api/v1/admin/groups", headers=admin)).json()[0]
    await client.post("/api/v1/admin/groups", headers=admin, json={"name": "第二组"})
    g2 = next(
        g for g in (await client.get("/api/v1/admin/groups", headers=admin)).json() if g["name"] == "第二组"
    )
    await grant(client, admin, document, "groups", [g1["id"], g2["id"]])
    assert (await client.get("/api/v1/documents?kb_id=" + document["kb_id"], headers=user[1])).json() == []
    await client.put(
        "/api/v1/admin/groups/" + g2["id"] + "/members", headers=admin, json={"user_ids": [user[0]["id"]]}
    )
    assert (
        len((await client.get("/api/v1/documents?kb_id=" + document["kb_id"], headers=user[1])).json()) == 1
    )
    await client.put("/api/v1/admin/groups/" + g2["id"] + "/members", headers=admin, json={"user_ids": []})
    assert (await client.get("/api/v1/files/" + document["version_id"], headers=user[1])).status_code == 404


async def test_grant_failure_rolls_back_policy_revision_and_audit(client, admin, user, document, database):
    await grant(client, admin, document)
    with psycopg.connect(database) as conn:
        before = conn.execute("SELECT revision FROM authorization_state").fetchone()[0]
        audits = conn.execute("SELECT count(*) FROM audit_events").fetchone()[0]
    result = await client.put(
        "/api/v1/admin/documents/" + document["document_id"] + "/access",
        headers=admin,
        json={"visibility": "groups", "group_ids": ["missing-group"]},
    )
    assert result.status_code == 404
    assert (
        len((await client.get("/api/v1/documents?kb_id=" + document["kb_id"], headers=user[1])).json()) == 1
    )
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT revision FROM authorization_state").fetchone()[0] == before
        assert conn.execute("SELECT count(*) FROM audit_events").fetchone()[0] == audits


async def test_upload_duplicate_confirmation_and_cleanup(client, admin, document, app):
    root = app.state.services.catalog.files.root
    assert len(list(root.iterdir())) == 1
    args = {
        "headers": admin,
        "data": {"kb_id": document["kb_id"], "title": "duplicate"},
        "files": {"file": ("manual.txt", b"private evidence")},
    }
    result = await client.post("/api/v1/documents", **args)
    assert result.status_code == 409 and result.json()["code"] == "duplicate_content"
    assert len(list(root.iterdir())) == 1
    args["data"]["confirm_duplicate"] = "true"
    assert (await client.post("/api/v1/documents", **args)).status_code == 202
    assert len(list(root.iterdir())) == 2


async def test_failed_upload_and_file_integrity(client, admin, document, app, database):
    root = app.state.services.catalog.files.root
    result = await client.post(
        "/api/v1/documents",
        headers=admin,
        data={"kb_id": "missing", "title": "bad"},
        files={"file": ("bad.txt", b"unique")},
    )
    assert result.status_code == 404 and len(list(root.iterdir())) == 1
    with psycopg.connect(database) as conn:
        key = conn.execute(
            "SELECT storage_key FROM document_versions WHERE id=%s", (document["version_id"],)
        ).fetchone()[0]
    (root / key).write_bytes(b"tampered")
    result = await client.get("/api/v1/files/" + document["version_id"], headers=admin)
    assert result.status_code == 409 and result.json()["code"] == "file_integrity"


@pytest.mark.parametrize(
    "name,content,status",
    [("empty.txt", b"", 400), ("bad.exe", b"x", 400), ("large.txt", b"x" * (20 * 1024 * 1024 + 1), 413)],
    ids=["empty", "unsupported", "oversize"],
)
async def test_upload_boundaries(client, admin, document, name, content, status):
    result = await client.post(
        "/api/v1/documents",
        headers=admin,
        data={"kb_id": document["kb_id"], "title": "bad"},
        files={"file": (name, content)},
    )
    assert result.status_code == status


async def test_version_parent_and_disable_boundary(client, admin, user, document):
    await grant(client, admin, document)
    path = "/api/v1/documents/wrong/versions/" + document["version_id"] + "/chunks"
    assert (await client.get(path, headers=admin)).status_code == 404
    result = await client.patch(
        "/api/v1/documents/" + document["document_id"] + "/enabled", headers=admin, json={"enabled": False}
    )
    assert result.status_code == 204
    assert (await client.get("/api/v1/documents?kb_id=" + document["kb_id"], headers=user[1])).json() == []
    assert (await client.get("/api/v1/files/" + document["version_id"], headers=user[1])).status_code == 404


async def test_job_retry_only_failed(client, admin, document, database):
    path = "/api/v1/ingestion-jobs/" + document["job_id"] + "/retry"
    assert (await client.post(path, headers=admin)).status_code == 409
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE ingestion_jobs SET status='failed',attempts=3,error='failed'")
    assert (await client.post(path, headers=admin)).status_code == 202
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT status FROM ingestion_jobs").fetchone()[0] == "queued"
    assert (await client.post(path, headers=admin)).status_code == 409


async def test_session_owner_scope_and_unknown_ids(client, admin, user, document):
    assert (
        await client.post("/api/v1/sessions", headers=user[1], json={"kb_ids": [document["kb_id"]]})
    ).status_code == 400
    await grant(client, admin, document)
    session = (
        await client.post("/api/v1/sessions", headers=user[1], json={"kb_ids": [document["kb_id"]]})
    ).json()
    assert (await client.get("/api/v1/sessions", headers=user[1])).json()[0]["kb_ids"] == [document["kb_id"]]
    assert (
        await client.get("/api/v1/sessions/" + session["id"] + "/messages", headers=admin)
    ).status_code == 404
    assert (
        await client.post("/api/v1/sessions", headers=user[1], json={"kb_ids": ["unknown"]})
    ).status_code == 400
    assert (
        await client.post(
            "/api/v1/sessions", headers=user[1], json={"kb_ids": [document["kb_id"]], "kb_id": "other"}
        )
    ).status_code == 400


async def test_admission_idempotency_and_single_generation(client, admin, user, document, app):
    await grant(client, admin, document)
    session = (await client.post("/api/v1/sessions", headers=user[1], json={})).json()
    svc = app.state.services
    principal = await svc.identity.authenticate(user[1]["Authorization"].split()[1])
    results = await asyncio.gather(
        *(svc.conversations.admit(principal, session["id"], "same", "question") for _ in range(6))
    )
    assert sum(created for _, created in results) == 1
    assert len({r["id"] for r, _ in results}) == 1
    with pytest.raises(AppError, match="进行中"):
        await svc.conversations.admit(principal, session["id"], "other", "question")
    with pytest.raises(AppError, match="其他问题"):
        await svc.conversations.admit(principal, session["id"], "same", "different question")
    message = results[0][0]
    await svc.conversations.finish(message["id"], MessageStatus.ERROR, "must not persist", [], {})
    with pytest.raises(AppError, match="终止"):
        await svc.conversations.finish(message["id"], MessageStatus.COMPLETED, "overwrite", [], {})


async def test_history_is_masked_after_revoke(client, admin, user, document, app):
    await grant(client, admin, document)
    session = (await client.post("/api/v1/sessions", headers=user[1], json={})).json()
    svc = app.state.services
    principal = await svc.identity.authenticate(user[1]["Authorization"].split()[1])
    message, _ = await svc.conversations.admit(principal, session["id"], "request-1", "用户自己的问题")
    await svc.conversations.finish(
        message["id"],
        MessageStatus.COMPLETED,
        "秘密配置",
        [{"version_id": document["version_id"]}],
        {"version_ids": [document["version_id"]], "kb_ids": [document["kb_id"]]},
    )
    path = "/api/v1/sessions/" + session["id"] + "/messages"
    assert (await client.get(path, headers=user[1])).json()[0]["answer"] == "秘密配置"
    await grant(client, admin, document, "groups")
    row = (await client.get(path, headers=user[1])).json()[0]
    assert (
        row["status"] == "restricted"
        and row["answer"] == ""
        and row["citations"] == []
        and row["trace"] == {}
    )
    assert row["question"] == "用户自己的问题"


async def test_request_schema_and_chat_not_ready(client, admin):
    assert (
        await client.patch("/api/v1/documents/unknown/enabled", headers=admin, json={"enabled": "false"})
    ).status_code == 400
    result = await client.post(
        "/api/v1/chat", headers=admin, json={"session_id": "x", "request_id": "x", "question": "问题"}
    )
    assert result.status_code == 503 and result.json()["code"] == "models_not_configured"
    result = await client.post(
        "/api/v1/chat",
        headers=admin,
        json={"session_id": "x", "request_id": "x", "question": "问题", "principal_id": "admin"},
    )
    assert result.status_code == 400
