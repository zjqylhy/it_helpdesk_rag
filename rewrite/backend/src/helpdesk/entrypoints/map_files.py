import argparse
import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from helpdesk.adapters.files.local import LocalFiles
from helpdesk.adapters.postgres.migration import API_LOCK, MIGRATION_LOCK


async def map_files(dsn: str, source_root: Path, target_root: Path, mapping: dict[str, str]) -> int:
    source_root = await asyncio.to_thread(source_root.resolve)
    files = LocalFiles(target_root)
    count = 0
    # 显式版本→相对文件映射，避免根据历史 Windows/Linux 绝对路径猜测来源。
    conn = await psycopg.AsyncConnection.connect(dsn, row_factory=dict_row)
    try:
        async with conn:
            row = await (
                await conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (API_LOCK,))
            ).fetchone()
            if not row or not row["ok"]:
                raise RuntimeError("API 正在运行，拒绝文件迁移")
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
            created = []
            try:
                for version_id, relative in mapping.items():
                    if Path(relative).is_absolute():
                        raise ValueError("映射必须使用来源根目录内的相对路径")
                    path = (source_root / relative).resolve()
                    if not path.is_relative_to(source_root):
                        raise ValueError("映射越出来源根目录")
                    row = await (
                        await conn.execute(
                            "SELECT * FROM document_versions WHERE id=%s FOR UPDATE", (version_id,)
                        )
                    ).fetchone()
                    if not row:
                        raise ValueError("映射中的文档版本不存在")
                    if row["storage_key"]:
                        await files.read(row["storage_key"], row["content_hash"])
                        continue

                    async def parts(source: Path = path) -> AsyncIterator[bytes]:
                        with source.open("rb") as stream:
                            while block := await asyncio.to_thread(stream.read, 64 * 1024):
                                yield block

                    key, digest, _ = await files.save(parts(), path.suffix.lower())
                    created.append(key)
                    if digest != row["content_hash"]:
                        raise ValueError("来源文件哈希与版本记录不一致")
                    await conn.execute(
                        "UPDATE document_versions SET storage_key=%s WHERE id=%s", (key, version_id)
                    )
                    count += 1
            except BaseException:
                # 尚未提交，先回滚再清理本批新文件；提交确认未知时由离线核对处理。
                await conn.rollback()
                for key in created:
                    await files.remove(key)
                raise
    finally:
        await conn.close()
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="在副本验证后显式映射历史文件，不修改旧原文件或旧 path 字段")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--target-root", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    args = parser.parse_args()
    dsn = os.environ.get("HD_DATABASE_URL")
    if not dsn:
        parser.error("必须设置 HD_DATABASE_URL")
    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    if not isinstance(mapping, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()
    ):
        parser.error("映射必须是版本 ID 到相对路径的 JSON 对象")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    count = asyncio.run(map_files(dsn, args.source_root, args.target_root, mapping))
    print("已映射并校验文件数：" + str(count))


if __name__ == "__main__":
    main()
