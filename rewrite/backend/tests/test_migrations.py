import hashlib
import json
import shutil
from pathlib import Path

import psycopg
import pytest
from conftest import MIGRATIONS
from psycopg import sql

from helpdesk.adapters.http.app import create_app
from helpdesk.adapters.postgres.migration import migrate
from helpdesk.settings import Settings


def test_migration_is_atomic_idempotent_and_checksum_locked(database, tmp_path):
    assert migrate(database, MIGRATIONS) == []
    changed = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS, changed)
    with (changed / "0001_foundation.sql").open("a", encoding="utf-8") as file:
        file.write("\n-- changed\n")
    with pytest.raises(RuntimeError, match="校验和"):
        migrate(database, changed)
    (changed / "0001_foundation.sql").write_bytes((MIGRATIONS / "0001_foundation.sql").read_bytes())
    (changed / "0004_invalid.sql").write_text(
        "CREATE TABLE must_rollback(id int); SELECT non_existing_column;", encoding="utf-8"
    )
    with pytest.raises(psycopg.Error):
        migrate(database, changed)
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT to_regclass('must_rollback')").fetchone()[0] is None
        assert conn.execute("SELECT max(version) FROM app_migrations").fetchone()[0] == 3


def test_legacy_schema_upgrade_preserves_data_and_vectors(empty_database):
    fixture = Path(__file__).parent / "fixtures/legacy-v2-schema.sql"
    with psycopg.connect(empty_database) as conn:
        conn.execute(fixture.read_text(encoding="utf-8"), prepare=False)
        conn.execute(
            "INSERT INTO users(id,username,password_hash,role) VALUES('old-user','old','unchanged-hash','user')"
        )
        conn.execute("INSERT INTO knowledge_bases(id,name,organization) VALUES('old-kb','旧库','旧组织')")
        conn.execute(
            "INSERT INTO documents(id,kb_id,title,visibility,active_version) VALUES('old-doc','old-kb','旧文档','all_users','old-version')"
        )
        conn.execute("""INSERT INTO document_versions(id,document_id,filename,path,content_hash,status,dimension,embedding_model)
            VALUES('old-version','old-doc','old.txt','C:/legacy/old.txt','old-hash','completed',3,'frozen-model')""")
        conn.execute("""INSERT INTO chunks(id,version_id,ordinal,text,locator,content_hash,embedding)
            VALUES('old-chunk','old-version',0,'旧原文','{"paragraph":1}','old-chunk-hash','[1,2,3]')""")
        conn.execute(
            "INSERT INTO snapshots(id,kb_id,revision,version_ids) VALUES('old-snapshot','old-kb',0,'[\"old-version\"]')"
        )
        conn.execute("INSERT INTO sessions(id,user_id,kb_id) VALUES('old-session','old-user','old-kb')")
        conn.execute("""INSERT INTO messages(id,session_id,request_id,question,answer,status,citations)
            VALUES('old-message','old-session','old-request','旧问题','旧答案','completed','[{"version_id":"old-version"}]')""")
        before = {
            table: conn.execute(
                sql.SQL("SELECT row_to_json(t)::text FROM {} t ORDER BY id").format(sql.Identifier(table))
            ).fetchall()
            for table in [
                "users",
                "knowledge_bases",
                "documents",
                "chunks",
                "snapshots",
                "sessions",
                "messages",
            ]
        }
    assert migrate(empty_database, MIGRATIONS) == [1, 2, 3]
    with psycopg.connect(empty_database) as conn:
        for table, rows in before.items():
            after = conn.execute(
                sql.SQL("SELECT row_to_json(t)::text FROM {} t ORDER BY id").format(sql.Identifier(table))
            ).fetchall()
            assert len(after) == len(rows)
            for before_row, after_row in zip(rows, after, strict=True):
                old, new = json.loads(before_row[0]), json.loads(after_row[0])
                assert {key: new[key] for key in old} == old
                assert set(new) - set(old) == ({"completion_kind"} if table == "messages" else set())
                if table == "messages":
                    assert new["completion_kind"] is None
        row = conn.execute(
            "SELECT path,content_hash,storage_key FROM document_versions WHERE id='old-version'"
        ).fetchone()
        assert row == ("C:/legacy/old.txt", "old-hash", None)
        assert (
            conn.execute("SELECT embedding::text FROM chunks WHERE id='old-chunk'").fetchone()[0] == "[1,2,3]"
        )


async def test_startup_never_migrates_or_resets_password(app, database, tmp_path):
    with psycopg.connect(database) as conn:
        before = conn.execute("SELECT password_hash FROM users WHERE username='admin'").fetchone()[0]
    second = create_app(Settings(database_url=database, upload_dir=tmp_path / "other"))
    with pytest.raises(RuntimeError, match="一个 API"):
        async with second.router.lifespan_context(second):
            pass
    with pytest.raises(RuntimeError, match="正在运行"):
        migrate(database, MIGRATIONS)
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT password_hash FROM users WHERE username='admin'").fetchone()[0] == before


async def test_missing_migration_fails_without_ddl(empty_database, tmp_path):
    app = create_app(Settings(database_url=empty_database, upload_dir=tmp_path / "files"))
    with pytest.raises(psycopg.errors.UndefinedTable):
        async with app.router.lifespan_context(app):
            pass
    with psycopg.connect(empty_database) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
            ).fetchone()[0]
            == 0
        )


def test_migration_hashes_match_committed_contract():
    # 清单绑定真实文件字节，Windows 换行转换也不能悄悄改变已执行迁移。
    import json

    manifest = json.loads((MIGRATIONS / "manifest.json").read_text(encoding="utf-8"))
    for file, digest in manifest.items():
        assert hashlib.sha256((MIGRATIONS / file).read_bytes()).hexdigest() == digest
