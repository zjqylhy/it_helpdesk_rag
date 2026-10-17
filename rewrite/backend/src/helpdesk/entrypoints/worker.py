import argparse
import asyncio

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from helpdesk.adapters.files.local import LocalFiles
from helpdesk.adapters.models.gateway import HttpModels
from helpdesk.adapters.parsing.process import parse_file
from helpdesk.adapters.postgres.jobs import JobStore
from helpdesk.adapters.postgres.runs import RunLedger
from helpdesk.application.ingestion import IngestionWorker
from helpdesk.domain.models import Row
from helpdesk.entrypoints.runtime import run
from helpdesk.settings import Settings


async def serve(settings: Settings, once: bool = False) -> None:
    if not settings.embedding_url or not settings.embedding_model:
        raise RuntimeError("Worker 需要显式配置向量模型端点与名称")
    if settings.worker_heartbeat_seconds >= settings.worker_lease_seconds / 2:
        raise RuntimeError("心跳间隔必须小于租约的一半")
    pool: AsyncConnectionPool[psycopg.AsyncConnection[Row]] = AsyncConnectionPool(
        settings.database_url.get_secret_value(),
        open=False,
        min_size=1,
        max_size=4,
        timeout=5,
        kwargs={"row_factory": dict_row, "connect_timeout": 5, "options": "-c statement_timeout=5000"},
    )
    async with pool, httpx.AsyncClient(timeout=25, trust_env=False, follow_redirects=False) as client:
        await pool.wait(timeout=5)
        async with pool.connection() as conn:
            row = await (await conn.execute("SELECT max(version) AS version FROM app_migrations")).fetchone()
            if not row or row["version"] != 3:
                raise RuntimeError("请先显式执行数据库迁移")
        worker = IngestionWorker(
            JobStore(pool, settings.worker_lease_seconds),
            LocalFiles(settings.upload_dir),
            HttpModels(settings, client),
            RunLedger(pool),
            settings,
            parse_file,
        )
        while True:
            worked = await worker.once()
            if once:
                return
            if not worked:
                await asyncio.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="独立入库进程，不启动 HTTP 或自动迁移")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    run(serve(Settings(), args.once))  # type: ignore[call-arg]


if __name__ == "__main__":
    main()
