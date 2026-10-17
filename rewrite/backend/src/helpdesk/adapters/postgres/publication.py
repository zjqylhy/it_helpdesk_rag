import hashlib
import json
import math

from helpdesk.domain.errors import AppError
from helpdesk.domain.models import new_id
from helpdesk.domain.rag import Lease, ParsedChunk

from .common import Connection, Queries, jsonb


async def refresh_snapshot(conn: Connection, kb_id: str) -> str:
    queries = Queries(conn)
    kb = await queries.require(
        "UPDATE knowledge_bases SET revision=revision+1 WHERE id=%s RETURNING revision", (kb_id,)
    )
    rows = await queries.many(
        """SELECT v.id FROM document_versions v JOIN documents d ON d.id=v.document_id
        WHERE d.kb_id=%s AND d.enabled AND d.active_version=v.id AND v.status='completed' ORDER BY v.id""",
        (kb_id,),
    )
    versions = [r["id"] for r in rows]
    snapshot_id = hashlib.sha256(json.dumps([kb_id, kb["revision"], versions]).encode()).hexdigest()
    await conn.execute(
        "INSERT INTO snapshots(id,kb_id,revision,version_ids) VALUES(%s,%s,%s,%s)",
        (snapshot_id, kb_id, kb["revision"], jsonb(versions)),
    )
    return snapshot_id


class Publication(Queries):
    async def publish(
        self, lease: Lease, chunks: list[ParsedChunk], vectors: list[list[float]], model: str, dimension: int
    ) -> None:
        if (
            len(chunks) != len(vectors)
            or not chunks
            or any(
                len(v) != dimension
                or not any(v)
                or any(type(x) not in {int, float} or not math.isfinite(x) for x in v)
                for v in vectors
            )
        ):
            raise AppError("invalid_publication", 500, "入库结果不完整")
        lease_row = await self.one(
            """SELECT id FROM ingestion_jobs WHERE id=%s AND status='running' AND lease_owner=%s
            AND attempts=%s AND lease_until>clock_timestamp() FOR UPDATE""",
            (lease.job_id, lease.owner, lease.attempt),
        )
        if lease_row is None:
            raise AppError("lease_lost", 409, "入库任务租约已失效")
        await self.require("SELECT id FROM knowledge_bases WHERE id=%s FOR UPDATE", (lease.kb_id,))
        document = await self.require("SELECT * FROM documents WHERE id=%s FOR UPDATE", (lease.document_id,))
        await self.conn.execute("DELETE FROM chunks WHERE version_id=%s", (lease.version_id,))
        for chunk, vector in zip(chunks, vectors, strict=True):
            await self.conn.execute(
                """INSERT INTO chunks(id,version_id,ordinal,text,locator,content_hash,embedding)
                VALUES(%s,%s,%s,%s,%s,%s,%s::vector)""",
                (
                    new_id(),
                    lease.version_id,
                    chunk.ordinal,
                    chunk.text,
                    jsonb(chunk.locator),
                    chunk.content_hash,
                    json.dumps(vector),
                ),
            )
        await self.conn.execute(
            """UPDATE document_versions SET status='completed',error=NULL,parser_version='rewrite-paragraph-700-120-v1',
            embedding_model=%s,dimension=%s WHERE id=%s""",
            (model, dimension, lease.version_id),
        )
        current = await self.one(
            "SELECT ingest_order FROM document_versions WHERE id=%s", (document["active_version"],)
        )
        if current is None or lease.ingest_order > current["ingest_order"]:
            await self.conn.execute(
                "UPDATE documents SET active_version=%s WHERE id=%s", (lease.version_id, lease.document_id)
            )
        await refresh_snapshot(self.conn, lease.kb_id)
        # 发布操作本身也可能超过租约，提交前再作一次栅栏检查。
        row = await self.one(
            """UPDATE ingestion_jobs SET status='completed',progress=100,error=NULL,
            lease_until=NULL,lease_owner=NULL,updated_at=now() WHERE id=%s AND lease_owner=%s
            AND attempts=%s AND lease_until>clock_timestamp() RETURNING id""",
            (lease.job_id, lease.owner, lease.attempt),
        )
        if row is None:
            raise AppError("lease_lost", 409, "入库任务租约已失效")
