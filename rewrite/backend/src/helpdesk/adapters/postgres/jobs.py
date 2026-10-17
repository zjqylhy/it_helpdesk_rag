import psycopg
from psycopg_pool import AsyncConnectionPool

from helpdesk.domain.errors import AppError
from helpdesk.domain.models import Row, new_id
from helpdesk.domain.rag import Lease, ParsedChunk

from .common import Queries
from .publication import Publication


class JobStore:
    def __init__(self, pool: AsyncConnectionPool[psycopg.AsyncConnection[Row]], seconds: int = 90) -> None:
        self.pool, self.seconds = pool, seconds

    async def claim(self) -> Lease | None:
        async with self.pool.connection() as conn:
            queries = Queries(conn)
            exhausted = await queries.many("""UPDATE ingestion_jobs SET status='failed',error='lease_attempts_exhausted',
                lease_owner=NULL,lease_until=NULL,updated_at=now() WHERE status='running' AND lease_until<clock_timestamp()
                AND attempts>=3 RETURNING version_id""")
            for expired in exhausted:
                await conn.execute(
                    "UPDATE document_versions SET status='failed',error='lease_attempts_exhausted' WHERE id=%s",
                    (expired["version_id"],),
                )
            row = await queries.one("""SELECT j.id AS job_id,j.version_id,v.document_id,d.kb_id,v.storage_key,
                v.filename,v.content_hash,v.ingest_order,j.attempts FROM ingestion_jobs j
                JOIN document_versions v ON v.id=j.version_id JOIN documents d ON d.id=v.document_id
                WHERE (j.status='queued' OR (j.status='running' AND j.lease_until<clock_timestamp())) AND j.attempts<3
                ORDER BY v.ingest_order FOR UPDATE OF j SKIP LOCKED LIMIT 1""")
            if row is None:
                return None
            if not row["storage_key"]:
                await conn.execute(
                    "UPDATE ingestion_jobs SET status='failed',error='storage_mapping_required' WHERE id=%s",
                    (row["job_id"],),
                )
                await conn.execute(
                    "UPDATE document_versions SET status='failed',error='storage_mapping_required' WHERE id=%s",
                    (row["version_id"],),
                )
                return None
            owner = new_id()
            await conn.execute(
                """UPDATE ingestion_jobs SET status='running',attempts=attempts+1,
                lease_owner=%s,lease_until=now()+%s*interval '1 second',error=NULL,progress=0,updated_at=now() WHERE id=%s""",
                (owner, self.seconds, row["job_id"]),
            )
            await conn.execute(
                "UPDATE document_versions SET status='processing',error=NULL WHERE id=%s",
                (row["version_id"],),
            )
            return Lease(
                row["job_id"],
                row["version_id"],
                row["document_id"],
                row["kb_id"],
                owner,
                row["attempts"] + 1,
                row["storage_key"],
                row["filename"],
                row["content_hash"],
                row["ingest_order"],
            )

    async def heartbeat(self, lease: Lease, progress: int = 0) -> None:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    """UPDATE ingestion_jobs SET lease_until=now()+%s*interval '1 second',
                progress=GREATEST(progress,%s),updated_at=now() WHERE id=%s AND status='running' AND lease_owner=%s
                AND attempts=%s AND lease_until>clock_timestamp() RETURNING id""",
                    (self.seconds, progress, lease.job_id, lease.owner, lease.attempt),
                )
            ).fetchone()
            if row is None:
                raise AppError("lease_lost", 409, "入库任务租约已失效")

    async def fail(self, lease: Lease, code: str) -> None:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    """UPDATE ingestion_jobs SET status='failed',error=%s,lease_owner=NULL,
                lease_until=NULL,updated_at=now() WHERE id=%s AND status='running' AND lease_owner=%s
                AND attempts=%s AND lease_until>clock_timestamp() RETURNING id""",
                    (code, lease.job_id, lease.owner, lease.attempt),
                )
            ).fetchone()
            if row:
                await conn.execute(
                    "UPDATE document_versions SET status='failed',error=%s WHERE id=%s",
                    (code, lease.version_id),
                )

    async def publish(
        self, lease: Lease, chunks: list[ParsedChunk], vectors: list[list[float]], model: str, dimension: int
    ) -> None:
        async with self.pool.connection() as conn:
            await Publication(conn).publish(lease, chunks, vectors, model, dimension)
