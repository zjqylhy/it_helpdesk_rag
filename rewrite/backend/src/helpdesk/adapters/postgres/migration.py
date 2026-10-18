import hashlib
from pathlib import Path

import psycopg

MIGRATION_LOCK = 731904002
API_LOCK = 731904003


def migrate(dsn: str, directory: Path) -> list[int]:
    files = sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    if not files:
        raise RuntimeError("未找到迁移文件")
    applied = []
    with psycopg.connect(dsn) as conn:
        acquired = conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (API_LOCK,)).fetchone()
        if acquired is None or not acquired[0]:
            raise RuntimeError("API 正在运行，拒绝在线迁移")
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
        conn.execute("""CREATE TABLE IF NOT EXISTS app_migrations(
            version integer PRIMARY KEY,sha256 text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())""")
        recorded: dict[int, str] = dict(conn.execute("SELECT version,sha256 FROM app_migrations").fetchall())
        known = {int(f.name[:4]) for f in files}
        if len(known) != len(files) or set(recorded) - known:
            raise RuntimeError("迁移版本重复或数据库版本高于当前程序")
        for file in files:
            version = int(file.name[:4])
            digest = hashlib.sha256(file.read_bytes()).hexdigest()
            if version in recorded:
                if recorded[version] != digest:
                    raise RuntimeError(f"已执行迁移 {version} 的校验和发生变化")
                continue
            conn.execute(file.read_text(encoding="utf-8"), prepare=False)
            conn.execute("INSERT INTO app_migrations(version,sha256) VALUES(%s,%s)", (version, digest))
            applied.append(version)
    return applied
