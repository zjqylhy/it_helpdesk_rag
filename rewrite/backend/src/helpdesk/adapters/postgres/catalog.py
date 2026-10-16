from helpdesk.domain.errors import conflict
from helpdesk.domain.models import Principal, Row, new_id

from .common import VISIBLE, Queries


class Catalog(Queries):
    async def storage_key_in_use(self, key: str) -> bool:
        return bool(await self.one("SELECT id FROM document_versions WHERE storage_key=%s", (key,)))

    async def knowledge_bases(self, principal: Principal) -> list[Row]:
        return await self.many(
            f"""SELECT k.* FROM knowledge_bases k WHERE %s='admin' OR EXISTS(
            SELECT 1 FROM documents d WHERE d.kb_id=k.id AND {VISIBLE}) ORDER BY k.name,k.id""",
            (principal.role, principal.role, principal.user_id),
        )

    async def create_kb(self, name: str, organization: str) -> Row:
        return await self.require(
            "INSERT INTO knowledge_bases(id,name,organization) VALUES(%s,%s,%s) RETURNING *",
            (new_id(), name, organization),
        )

    async def default_search(self, kb_id: str, enabled: bool) -> Row:
        return await self.require(
            "UPDATE knowledge_bases SET default_search=%s WHERE id=%s RETURNING *", (enabled, kb_id)
        )

    async def documents(self, principal: Principal, kb_id: str | None) -> list[Row]:
        return await self.many(
            f"""SELECT d.*,a.status AS active_status,v.id AS latest_version,v.filename,v.error,
            v.created_at AS version_created_at,j.id AS job_id,j.status AS job_status,j.progress
            FROM documents d LEFT JOIN document_versions a ON a.id=d.active_version
            LEFT JOIN LATERAL (SELECT * FROM document_versions WHERE document_id=d.id
                ORDER BY created_at DESC,id DESC LIMIT 1) v ON true
            LEFT JOIN ingestion_jobs j ON j.version_id=v.id WHERE {VISIBLE}
            AND d.kb_id=%s ORDER BY d.created_at,d.id""",
            (principal.role, principal.user_id, kb_id),
        )

    async def document(self, principal: Principal, document_id: str, *, lock: bool = False) -> Row:
        return await self.require(
            f"SELECT d.* FROM documents d WHERE d.id=%s AND {VISIBLE}" + (" FOR UPDATE" if lock else ""),
            (document_id, principal.role, principal.user_id),
        )

    async def versions(self, document_id: str) -> list[Row]:
        return await self.many(
            """SELECT v.*,j.id AS job_id,j.status AS job_status,j.progress,j.attempts
            FROM document_versions v LEFT JOIN ingestion_jobs j ON j.version_id=v.id
            WHERE v.document_id=%s ORDER BY v.created_at,v.id""",
            (document_id,),
        )

    async def version(self, principal: Principal, version_id: str) -> Row:
        return await self.require(
            f"""SELECT v.*,d.kb_id FROM document_versions v
            JOIN documents d ON d.id=v.document_id WHERE v.id=%s AND {VISIBLE}""",
            (version_id, principal.role, principal.user_id),
        )

    async def duplicate(self, kb_id: str, content_hash: str) -> bool:
        # 同库上传串行化，重复检测与版本入队保持在同一事务。
        await self.require("SELECT id FROM knowledge_bases WHERE id=%s FOR UPDATE", (kb_id,))
        return bool(
            await self.one(
                """SELECT v.id FROM document_versions v JOIN documents d ON d.id=v.document_id
            WHERE d.kb_id=%s AND v.content_hash=%s LIMIT 1""",
                (kb_id, content_hash),
            )
        )

    async def queue_document(self, kb_id: str, document_id: str | None, values: Row) -> Row:
        if document_id is None:
            document_id = new_id()
            await self.conn.execute(
                "INSERT INTO documents(id,kb_id,title,visibility) VALUES(%s,%s,%s,'groups')",
                (document_id, kb_id, values["title"]),
            )
        version_id, job_id = new_id(), new_id()
        await self.conn.execute(
            """INSERT INTO document_versions
            (id,document_id,filename,path,storage_key,content_hash,source) VALUES(%s,%s,%s,%s,%s,%s,%s)""",
            (
                version_id,
                document_id,
                values["filename"],
                values["storage_key"],
                values["storage_key"],
                values["content_hash"],
                values["source"],
            ),
        )
        await self.conn.execute(
            "INSERT INTO ingestion_jobs(id,version_id) VALUES(%s,%s)", (job_id, version_id)
        )
        return {"document_id": document_id, "version_id": version_id, "job_id": job_id, "status": "queued"}

    async def set_enabled(self, document_id: str, enabled: bool) -> Row:
        return await self.require(
            "UPDATE documents SET enabled=%s WHERE id=%s RETURNING *", (enabled, document_id)
        )

    async def retry(self, job_id: str) -> Row:
        row = await self.require("SELECT * FROM ingestion_jobs WHERE id=%s FOR UPDATE", (job_id,))
        if row["status"] != "failed":
            raise conflict("只能重试已失败的入库任务")
        await self.conn.execute(
            "UPDATE document_versions SET status='queued',error=NULL WHERE id=%s", (row["version_id"],)
        )
        return await self.require(
            """UPDATE ingestion_jobs SET status='queued',attempts=0,progress=0,error=NULL,
            lease_until=NULL,lease_owner=NULL,updated_at=now() WHERE id=%s RETURNING *""",
            (job_id,),
        )

    async def chunks(self, document_id: str, version_id: str) -> list[Row]:
        await self.require(
            "SELECT id FROM document_versions WHERE id=%s AND document_id=%s", (version_id, document_id)
        )
        return await self.many(
            """SELECT id,version_id,ordinal,text,locator,content_hash FROM chunks
            WHERE version_id=%s ORDER BY ordinal""",
            (version_id,),
        )
