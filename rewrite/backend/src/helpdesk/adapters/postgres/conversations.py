from helpdesk.domain.errors import conflict
from helpdesk.domain.models import Principal, Row, new_id

from .catalog import Catalog
from .common import VISIBLE, Queries, jsonb


class Conversations(Queries):
    async def create_session(self, principal: Principal, kb_ids: list[str]) -> Row:
        return await self.require(
            """INSERT INTO sessions(id,user_id,kb_id,kb_ids) VALUES(%s,%s,%s,%s) RETURNING *""",
            (new_id(), principal.user_id, kb_ids[0] if len(kb_ids) == 1 else None, jsonb(kb_ids)),
        )

    async def sessions(self, principal: Principal) -> list[Row]:
        return await self.many(
            "SELECT * FROM sessions WHERE user_id=%s ORDER BY created_at DESC,id", (principal.user_id,)
        )

    async def session(self, principal: Principal, session_id: str) -> Row:
        return await self.require(
            "SELECT * FROM sessions WHERE id=%s AND user_id=%s", (session_id, principal.user_id)
        )

    async def messages(self, session_id: str) -> list[Row]:
        return await self.many(
            "SELECT * FROM messages WHERE session_id=%s ORDER BY created_at,id", (session_id,)
        )

    async def provenance_visible(self, principal: Principal, provenance: Row) -> bool:
        if principal.role == "admin":
            return True
        version_ids = provenance.get("version_ids", [])
        kb_ids = provenance.get("kb_ids", [])
        if not isinstance(version_ids, list) or not isinstance(kb_ids, list):
            return False
        if version_ids:
            rows = await self.many(
                f"""SELECT v.id FROM document_versions v JOIN documents d ON d.id=v.document_id
                WHERE v.id=ANY(%s) AND {VISIBLE}""",
                (version_ids, principal.role, principal.user_id),
            )
            if {r["id"] for r in rows} != set(version_ids):
                return False
        if kb_ids:
            visible = await Catalog(self.conn).knowledge_bases(principal)
            if not set(kb_ids).issubset({r["id"] for r in visible}):
                return False
        # 新请求可以明确绑定空范围，只会产生无资料拒答；旧历史无绑定信息时仍遮蔽。
        scope_hash = provenance.get("scope_hash")
        bound_empty = (
            not version_ids
            and not kb_ids
            and provenance.get("snapshot_members") == []
            and isinstance(scope_hash, str)
            and len(scope_hash) == 64
        )
        return bool(version_ids or kb_ids or bound_empty)

    async def admit(
        self, session_id: str, request_id: str, question: str, principal: Principal
    ) -> tuple[Row, bool]:
        await self.require(
            "SELECT id FROM sessions WHERE id=%s AND user_id=%s FOR UPDATE", (session_id, principal.user_id)
        )
        existing = await self.one(
            "SELECT * FROM messages WHERE session_id=%s AND request_id=%s", (session_id, request_id)
        )
        if existing:
            if existing["question"] != question:
                raise conflict("request_id 已用于其他问题")
            return existing, False
        if await self.one(
            "SELECT id FROM messages WHERE session_id=%s AND status='generating'", (session_id,)
        ):
            raise conflict("同会话已有进行中的请求")
        row = await self.require(
            """INSERT INTO messages(id,session_id,request_id,question,usage_kind,strategy_version)
            VALUES(%s,%s,%s,%s,%s,'rewrite-m1') RETURNING *""",
            (new_id(), session_id, request_id, question, principal.usage_kind),
        )
        return row, True

    async def finish(
        self, message_id: str, status: str, answer: str, citations: list[Row], provenance: Row
    ) -> Row:
        row = await self.one(
            """UPDATE messages SET status=%s,answer=%s,citations=%s,provenance=%s
            WHERE id=%s AND status='generating' RETURNING *""",
            (status, answer, jsonb(citations), jsonb(provenance), message_id),
        )
        if row is None:
            raise conflict("消息已终止，不能覆盖结果")
        return row
