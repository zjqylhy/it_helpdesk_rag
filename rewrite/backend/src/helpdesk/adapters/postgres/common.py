from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from helpdesk.domain.errors import missing
from helpdesk.domain.models import Row

type Connection = AsyncConnection[Row]


class Queries:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    async def one(self, query: str, args: tuple[Any, ...] = ()) -> Row | None:
        return await (await self.conn.execute(query, args)).fetchone()

    async def require(self, query: str, args: tuple[Any, ...] = ()) -> Row:
        row = await self.one(query, args)
        if row is None:
            raise missing()
        return row

    async def many(self, query: str, args: tuple[Any, ...] = ()) -> list[Row]:
        return await (await self.conn.execute(query, args)).fetchall()


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value)


# 相同谓词同时用于元数据和原文入口，避免只在列表过滤而详情越权。
VISIBLE = """(%s='admin' OR (d.enabled AND (d.visibility='all_users' OR EXISTS(
 SELECT 1 FROM document_group_grants g JOIN user_group_members m USING(group_id)
 WHERE g.document_id=d.id AND m.user_id=%s))))"""
