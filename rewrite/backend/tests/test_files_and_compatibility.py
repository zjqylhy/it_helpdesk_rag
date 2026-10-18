import hashlib
import json
from pathlib import Path

import psycopg
import pytest

from helpdesk.adapters.files.local import LocalFiles
from helpdesk.domain.errors import AppError
from helpdesk.entrypoints.map_files import map_files


def shape(value):
    if isinstance(value, dict):
        return {key: shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    return type(value).__name__


async def test_external_v1_response_shapes(client, admin, document, user):
    fixture = json.loads((Path(__file__).parent / "fixtures/legacy-api.json").read_text(encoding="utf-8"))
    expected = {item["label"]: item for item in fixture["samples"]}
    cases = [
        ("me", "/api/v1/auth/me"),
        ("list_kbs", "/api/v1/knowledge-bases"),
        ("list_documents", "/api/v1/documents"),
        ("list_documents_in_kb", "/api/v1/documents?kb_id=" + document["kb_id"]),
        ("versions", "/api/v1/documents/" + document["document_id"] + "/versions"),
        ("admin_users", "/api/v1/admin/users"),
        ("access", "/api/v1/admin/documents/" + document["document_id"] + "/access"),
    ]
    for label, path in cases:
        response = await client.get(path, headers=admin)
        assert response.status_code == expected[label]["status"], label
        assert shape(response.json()) == shape(expected[label]["body"]), label
    session = await client.post("/api/v1/sessions", headers=admin, json={"kb_ids": [document["kb_id"]]})
    assert session.status_code == expected["create_session"]["status"]
    assert shape(session.json()) == shape(expected["create_session"]["body"])
    assert shape((await client.get("/api/v1/sessions", headers=admin)).json()) == shape(
        expected["list_sessions"]["body"]
    )


async def test_storage_key_and_explicit_mapping(database, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "target"
    content = b"legacy immutable bytes"
    (source / "old.txt").write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    with psycopg.connect(database) as conn:
        conn.execute("INSERT INTO knowledge_bases(id,name,organization) VALUES('k','kb','org')")
        conn.execute("INSERT INTO documents(id,kb_id,title) VALUES('d','k','doc')")
        conn.execute(
            "INSERT INTO document_versions(id,document_id,filename,path,content_hash) VALUES('v','d','old.txt','untrusted/old.txt',%s)",
            (digest,),
        )
    assert await map_files(database, source, target, {"v": "old.txt"}) == 1
    assert await map_files(database, source, target, {"v": "old.txt"}) == 0
    with psycopg.connect(database) as conn:
        key, path = conn.execute("SELECT storage_key,path FROM document_versions WHERE id='v'").fetchone()
        assert path == "untrusted/old.txt"
    assert await LocalFiles(target).read(key, digest) == content
    assert (source / "old.txt").read_bytes() == content
    with pytest.raises(AppError):
        await LocalFiles(target).read("../old.txt", digest)


async def test_file_mapping_refuses_traversal_and_rolls_back_batch(database, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "target"
    (source / "ok.txt").write_bytes(b"ok")
    (source / "bad.txt").write_bytes(b"tampered")
    with psycopg.connect(database) as conn:
        conn.execute("INSERT INTO knowledge_bases(id,name,organization) VALUES('k','kb','org')")
        conn.execute("INSERT INTO documents(id,kb_id,title) VALUES('d','k','doc')")
        for version in ["v1", "v2"]:
            conn.execute(
                "INSERT INTO document_versions(id,document_id,filename,path,content_hash) VALUES(%s,'d','old.txt','old',%s)",
                (version, hashlib.sha256(b"ok").hexdigest()),
            )
    with pytest.raises(ValueError, match="越出"):
        await map_files(database, source, target, {"v1": "../other.txt"})
    with pytest.raises(ValueError, match="哈希"):
        await map_files(database, source, target, {"v1": "ok.txt", "v2": "bad.txt"})
    assert not list(target.iterdir())
    with psycopg.connect(database) as conn:
        assert (
            conn.execute("SELECT count(*) FROM document_versions WHERE storage_key IS NOT NULL").fetchone()[0]
            == 0
        )


async def test_bootstrap_cannot_reset_existing_accounts(app, database):
    service = app.state.services.identity
    with pytest.raises(AppError, match="空账号库"):
        await service.create(None, "admin", "DifferentPassword123!", role="admin", bootstrap=True)
    login = await service.login("admin", "AdminTest123!")
    assert login["role"] == "admin"
