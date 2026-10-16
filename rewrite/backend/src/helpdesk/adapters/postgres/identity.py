from helpdesk.domain.models import Principal, Row

from .common import Queries


class Identity(Queries):
    async def user_by_name(self, username: str) -> Row | None:
        return await self.one("SELECT * FROM users WHERE username=%s", (username,))

    async def user(self, user_id: str, *, lock: bool = False) -> Row | None:
        return await self.one("SELECT * FROM users WHERE id=%s" + (" FOR UPDATE" if lock else ""), (user_id,))

    async def principal(self, token_hash: str) -> Principal | None:
        row = await self.one(
            """SELECT u.id,u.username,u.role,u.usage_kind FROM tokens t
            JOIN users u ON u.id=t.user_id WHERE t.token_hash=%s AND t.expires_at>now() AND NOT u.disabled""",
            (token_hash,),
        )
        return Principal(row["id"], row["username"], row["role"], row["usage_kind"]) if row else None

    async def create_user(self, values: Row) -> Row:
        return await self.require(
            """INSERT INTO users(id,username,password_hash,role,usage_kind)
            VALUES(%s,%s,%s,%s,%s) RETURNING id,username,role,disabled,usage_kind""",
            tuple(values[k] for k in ("id", "username", "password_hash", "role", "usage_kind")),
        )

    async def save_token(self, digest: str, user_id: str, hours: int) -> None:
        await self.conn.execute(
            "INSERT INTO tokens VALUES(%s,%s,now()+%s*interval '1 hour')", (digest, user_id, hours)
        )

    async def delete_token(self, digest: str) -> None:
        await self.conn.execute("DELETE FROM tokens WHERE token_hash=%s", (digest,))

    async def users(self) -> list[Row]:
        return await self.many("SELECT id,username,role,disabled,usage_kind FROM users ORDER BY username,id")
