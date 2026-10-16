import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
import pytest_asyncio
from helpdesk.adapters.http.app import create_app
from helpdesk.adapters.postgres.migration import migrate
from helpdesk.settings import Settings
from psycopg import sql

REWRITE = Path(__file__).resolve().parents[2]
ROOT = REWRITE.parent
MIGRATIONS = REWRITE / "db/migrations"


def pytest_asyncio_loop_factories(config, item):
    return (
        {"selector": asyncio.SelectorEventLoop}
        if sys.platform == "win32"
        else {"default": asyncio.new_event_loop}
    )


@pytest.fixture
def empty_database():
    configured = os.environ.get("HD_TEST_DATABASE_URL")
    if configured:
        params = psycopg.conninfo.conninfo_to_dict(configured)
    else:
        state_path = ROOT / "data/rewrite-m1-postgres/state.json"
        if not state_path.exists():
            pytest.fail("请先运行 rewrite/scripts/test_postgres.py start，或配置独立 HD_TEST_DATABASE_URL")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        params = {
            "host": "127.0.0.1",
            "port": state["port"],
            "user": "postgres",
            "password": state["password"],
            "dbname": "postgres",
        }
    name = "m1test_" + uuid4().hex
    with psycopg.connect(**params, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = psycopg.conninfo.make_conninfo(**{**params, "dbname": name})
    try:
        yield dsn
    finally:
        assert name.startswith("m1test_") and len(name) == 39
        with psycopg.connect(**params, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def database(empty_database):
    migrate(empty_database, MIGRATIONS)
    return empty_database


@pytest_asyncio.fixture
async def app(database, tmp_path):
    instance = create_app(Settings(database_url=database, upload_dir=tmp_path / "files"))
    async with instance.router.lifespan_context(instance):
        await instance.state.services.identity.create(
            None, "admin", "AdminTest123!", role="admin", bootstrap=True
        )
        yield instance


@pytest_asyncio.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def admin(client):
    result = await client.post("/api/v1/auth/login", json={"username": "admin", "password": "AdminTest123!"})
    assert result.status_code == 200, result.text
    return {"Authorization": "Bearer " + result.json()["token"]}


@pytest_asyncio.fixture
async def user(client, admin):
    result = await client.post(
        "/api/v1/admin/users",
        headers=admin,
        json={"username": "alice", "password": "UserTest123!", "role": "user"},
    )
    assert result.status_code == 204, result.text
    created = next(
        x for x in (await client.get("/api/v1/admin/users", headers=admin)).json() if x["username"] == "alice"
    )
    login = await client.post("/api/v1/auth/login", json={"username": "alice", "password": "UserTest123!"})
    return created, {"Authorization": "Bearer " + login.json()["token"]}


@pytest_asyncio.fixture
async def document(client, admin):
    kb = await client.post(
        "/api/v1/knowledge-bases", headers=admin, json={"name": "测试知识库", "organization": "测试学校"}
    )
    assert kb.status_code == 201, kb.text
    upload = await client.post(
        "/api/v1/documents",
        headers=admin,
        data={"kb_id": kb.json()["id"], "title": "配置文档"},
        files={"file": ("manual.txt", b"private evidence", "text/plain")},
    )
    assert upload.status_code == 202, upload.text
    return {**upload.json(), "kb_id": kb.json()["id"]}


async def grant(client, admin, document, visibility="all_users", group_ids=None):
    result = await client.put(
        "/api/v1/admin/documents/" + document["document_id"] + "/access",
        headers=admin,
        json={"visibility": visibility, "group_ids": group_ids or []},
    )
    assert result.status_code == 204, result.text
    return None
