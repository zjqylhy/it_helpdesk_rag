import json
from dataclasses import replace

import psycopg
from psycopg_pool import AsyncConnectionPool

from helpdesk.domain.errors import AppError
from helpdesk.domain.models import Principal, Row
from helpdesk.domain.rag import BoundScope, Chunk, fingerprint
from helpdesk.settings import Settings

from .catalog import Catalog
from .common import VISIBLE, Queries

CHUNK_SELECT = """SELECT c.id,c.version_id,v.document_id,d.kb_id,k.name AS kb_name,k.organization,
 d.title,c.text,c.locator,c.content_hash FROM chunks c JOIN document_versions v ON v.id=c.version_id
 JOIN documents d ON d.id=v.document_id JOIN knowledge_bases k ON k.id=d.kb_id"""


class PostgresCorpus:
    def __init__(self, pool: AsyncConnectionPool[psycopg.AsyncConnection[Row]], settings: Settings) -> None:
        self.pool, self.settings = pool, settings

    async def bind(self, principal: Principal, kb_ids: tuple[str, ...], revision: int) -> BoundScope:
        async with self.pool.connection() as conn:
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            queries = Queries(conn)
            principal = await self._current(queries, principal)
            state = await queries.require("SELECT revision FROM authorization_state WHERE id=1")
            revision = int(state["revision"])
            available = await Catalog(conn).knowledge_bases(principal)
            allowed = {k["id"] for k in available}
            if not set(kb_ids).issubset(allowed):
                raise AppError("invalid_scope", 400, "会话知识库范围已不可用")
            selected = (
                [k for k in available if k["id"] in kb_ids]
                if kb_ids
                else [k for k in available if k["default_search"]]
            )
            if len(selected) > 32:
                raise AppError("scope_too_large", 400, "请指定不超过 32 个知识库")
            selected.sort(key=lambda kb: kb["id"])
            ids = [k["id"] for k in selected]
            versions = await queries.many(
                f"""SELECT v.id,v.dimension,v.embedding_model,d.kb_id FROM document_versions v
                JOIN documents d ON d.id=v.document_id WHERE d.active_version=v.id AND d.enabled
                AND v.status='completed' AND d.kb_id=ANY(%s) AND {VISIBLE} ORDER BY v.id""",
                (ids, principal.role, principal.user_id),
            )
            if any(
                v["dimension"] != self.settings.embedding_dimension
                or v["embedding_model"] != self.settings.embedding_model
                for v in versions
            ):
                raise AppError("embedding_identity_mismatch", 409, "资料向量配置与当前检索模型不一致")
            snapshots = await queries.many(
                """SELECT DISTINCT ON(kb_id) id,kb_id,revision,version_ids FROM snapshots
                WHERE kb_id=ANY(%s) ORDER BY kb_id,revision DESC""",
                (ids,),
            )
            members = tuple(v["id"] for v in versions)
            return BoundScope(
                tuple(ids),
                tuple(selected),
                members,
                tuple(snapshots),
                revision,
                fingerprint(
                    {"kb_ids": ids, "versions": members, "snapshots": snapshots, "revision": revision}
                ),
            )

    async def check(self, principal: Principal, scope: BoundScope) -> None:
        async with self.pool.connection() as conn:
            queries = Queries(conn)
            principal = await self._current(queries, principal)
            rows = await queries.many(
                f"""SELECT v.id FROM document_versions v JOIN documents d ON d.id=v.document_id
                WHERE v.id=ANY(%s) AND d.enabled AND {VISIBLE}""",
                (list(scope.version_ids), principal.role, principal.user_id),
            )
            if {r["id"] for r in rows} != set(scope.version_ids):
                raise AppError("authorization_changed", 409, "资料访问权限已变化")
            visible = {kb["id"] for kb in await Catalog(conn).knowledge_bases(principal)}
            if not set(scope.kb_ids).issubset(visible):
                raise AppError("authorization_changed", 409, "知识库访问权限已变化")

    async def _current(self, queries: Queries, principal: Principal) -> Principal:
        row = await queries.one("SELECT id,username,role,usage_kind,disabled FROM users WHERE id=%s",
                                (principal.user_id,))
        if not row or row["disabled"]:
            raise AppError("authorization_changed", 409, "账号已失效")
        return Principal(row["id"], row["username"], row["role"], row["usage_kind"])

    async def metadata(self, principal: Principal, scope: BoundScope) -> BoundScope:
        async with self.pool.connection() as conn:
            queries = Queries(conn)
            principal = await self._current(queries, principal)
            versions = await queries.many(
                f"""SELECT v.id,d.kb_id FROM document_versions v JOIN documents d ON d.id=v.document_id
                WHERE v.id=ANY(%s) AND d.kb_id=ANY(%s) AND d.enabled AND {VISIBLE} ORDER BY v.id""",
                (list(scope.version_ids), list(scope.kb_ids), principal.role, principal.user_id),
            )
            readable = {row["kb_id"] for row in versions}
            # 元数据沿用启动时的值，不将后来的增权、新版或组织变化混入旧任务。
            return replace(scope, version_ids=tuple(row["id"] for row in versions),
                           knowledge_bases=tuple(kb for kb in scope.knowledge_bases if kb["id"] in readable))

    async def chunks(self, principal: Principal, scope: BoundScope, kb_id: str) -> list[Chunk]:
        if kb_id not in scope.kb_ids:
            raise AppError("invalid_scope", 400, "检索范围无效")
        async with self.pool.connection() as conn:
            queries = Queries(conn)
            principal = await self._current(queries, principal)
            rows = await queries.many(
                CHUNK_SELECT
                + f""" WHERE c.version_id=ANY(%s) AND d.kb_id=%s
                AND d.enabled AND {VISIBLE} ORDER BY c.version_id,c.ordinal""",
                (list(scope.version_ids), kb_id, principal.role, principal.user_id),
            )
            return [Chunk(**r) for r in rows]

    async def export(self, principal: Principal, scope: BoundScope) -> list[Row]:
        """私有评测冻结；向量哈希参与漂移检查，不把向量写入答案或浏览器。"""
        await self.check(principal, scope)
        async with self.pool.connection() as conn:
            queries = Queries(conn)
            principal = await self._current(queries, principal)
            return await queries.many(
                CHUNK_SELECT.replace(" FROM chunks", ",md5(c.embedding::text) AS vector_hash FROM chunks")
                + f" WHERE c.version_id=ANY(%s) AND d.enabled AND {VISIBLE} ORDER BY c.id",
                (list(scope.version_ids), principal.role, principal.user_id),
            )

    async def vector(
        self, principal: Principal, scope: BoundScope, kb_id: str, embedding: list[float]
    ) -> list[Chunk]:
        if kb_id not in scope.kb_ids:
            raise AppError("invalid_scope", 400, "检索范围无效")
        async with self.pool.connection() as conn:
            queries = Queries(conn)
            principal = await self._current(queries, principal)
            rows = await queries.many(
                CHUNK_SELECT
                + f""" WHERE c.version_id=ANY(%s) AND d.kb_id=%s
                AND d.enabled AND {VISIBLE} ORDER BY c.embedding <=> %s::vector,c.id LIMIT 20""",
                (list(scope.version_ids), kb_id, principal.role, principal.user_id, json.dumps(embedding)),
            )
            return [Chunk(**r) for r in rows]

    async def review_export(self, principal: Principal, scope: BoundScope) -> list[Row]:
        """重新绑定的私有比对输入；不扩大原问答导出的字段或权限。"""
        await self.check(principal, scope)
        async with self.pool.connection() as conn:
            queries = Queries(conn)
            principal = await self._current(queries, principal)
            return await queries.many(
                CHUNK_SELECT.replace(
                    " FROM chunks",
                    ",c.ordinal,v.filename,v.source,v.embedding_model,v.dimension,"
                    "md5(c.embedding::text) AS vector_hash FROM chunks",
                )
                + f" WHERE c.version_id=ANY(%s) AND d.enabled AND {VISIBLE} ORDER BY c.id",
                (list(scope.version_ids), principal.role, principal.user_id),
            )
