from helpdesk.domain.errors import conflict, missing
from helpdesk.domain.models import Row, new_id

from .common import Queries, jsonb


class Access(Queries):
    async def update_user(self, user_id: str, usage_kind: str | None, password_hash: str | None) -> None:
        await self.require(
            """UPDATE users SET usage_kind=COALESCE(%s,usage_kind),
            password_hash=COALESCE(%s,password_hash) WHERE id=%s RETURNING id""",
            (usage_kind, password_hash, user_id),
        )
        if password_hash:
            await self.conn.execute("DELETE FROM tokens WHERE user_id=%s", (user_id,))

    async def revision(self, *, lock: bool = False) -> int:
        row = await self.require(
            "SELECT revision FROM authorization_state WHERE id=1" + (" FOR UPDATE" if lock else "")
        )
        return int(row["revision"])

    async def audit(self, actor: str, action: str, target: str, details: Row) -> int:
        row = await self.require(
            "UPDATE authorization_state SET revision=revision+1 WHERE id=1 RETURNING revision"
        )
        await self.conn.execute(
            "INSERT INTO audit_events(id,actor_id,action,target_id,details) VALUES(%s,%s,%s,%s,%s)",
            (new_id(), actor, action, target, jsonb(details)),
        )
        return int(row["revision"])

    async def groups(self) -> list[Row]:
        return await self.many("""SELECT g.id,g.name,COALESCE(jsonb_agg(m.user_id ORDER BY m.user_id)
            FILTER(WHERE m.user_id IS NOT NULL),'[]') AS user_ids FROM user_groups g
            LEFT JOIN user_group_members m ON m.group_id=g.id GROUP BY g.id ORDER BY g.name,g.id""")

    async def create_group(self, name: str) -> Row:
        return await self.require("INSERT INTO user_groups VALUES(%s,%s) RETURNING id,name", (new_id(), name))

    async def set_members(self, group_id: str, user_ids: list[str]) -> None:
        await self.require("SELECT id FROM user_groups WHERE id=%s FOR UPDATE", (group_id,))
        found = await self.many("SELECT id FROM users WHERE id=ANY(%s)", (user_ids,))
        if len(found) != len(user_ids):
            raise missing()
        await self.conn.execute("DELETE FROM user_group_members WHERE group_id=%s", (group_id,))
        for user_id in user_ids:
            await self.conn.execute("INSERT INTO user_group_members VALUES(%s,%s)", (group_id, user_id))

    async def set_document_access(self, document_id: str, visibility: str, groups: list[str]) -> None:
        await self.require("SELECT id FROM documents WHERE id=%s FOR UPDATE", (document_id,))
        found = await self.many("SELECT id FROM user_groups WHERE id=ANY(%s)", (groups,))
        if len(found) != len(groups):
            raise missing()
        await self.conn.execute("UPDATE documents SET visibility=%s WHERE id=%s", (visibility, document_id))
        await self.conn.execute("DELETE FROM document_group_grants WHERE document_id=%s", (document_id,))
        for group_id in groups:
            await self.conn.execute(
                "INSERT INTO document_group_grants VALUES(%s,%s)", (document_id, group_id)
            )

    async def disable_user(self, user_id: str, disabled: bool) -> None:
        row = await self.require("SELECT role,disabled FROM users WHERE id=%s FOR UPDATE", (user_id,))
        if disabled and row["role"] == "admin":
            active = await self.many("SELECT id FROM users WHERE role='admin' AND NOT disabled FOR UPDATE")
            if len(active) <= 1 and not row["disabled"]:
                raise conflict("不能停用最后一个管理员")
        await self.conn.execute("UPDATE users SET disabled=%s WHERE id=%s", (disabled, user_id))
        if disabled:
            await self.conn.execute("DELETE FROM tokens WHERE user_id=%s", (user_id,))

    async def events(self) -> list[Row]:
        return await self.many("SELECT * FROM audit_events ORDER BY created_at DESC,id LIMIT 200")

    async def document_access(self, document_id: str) -> Row:
        row = await self.require("SELECT id,visibility FROM documents WHERE id=%s", (document_id,))
        row["group_ids"] = [
            g["group_id"]
            for g in await self.many(
                "SELECT group_id FROM document_group_grants WHERE document_id=%s ORDER BY group_id",
                (document_id,),
            )
        ]
        return row
