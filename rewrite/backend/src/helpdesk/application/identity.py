import asyncio
import hashlib
import secrets

import bcrypt

from helpdesk.domain.errors import AppError, conflict
from helpdesk.domain.models import Principal, Row, new_id
from helpdesk.ports.repositories import Repositories, UnitOfWork


def digest_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def active(repos: Repositories, principal: Principal, *, admin: bool = False) -> Principal:
    row = await repos.identity.user(principal.user_id)
    if row is None or row["disabled"]:
        raise AppError("unauthorized", 401, "登录已失效")
    current = Principal(row["id"], row["username"], row["role"], row["usage_kind"])
    if admin:
        current.require_admin()
    return current


def check_password(password: str, encoded: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), encoded.encode())
    except (ValueError, TypeError):
        return False


class IdentityService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow
        self.dummy_hash = bcrypt.hashpw(b"unknown-account", bcrypt.gensalt()).decode()

    async def authenticate(self, token: str) -> Principal:
        async with self.uow() as repos:
            principal = await repos.identity.principal(digest_token(token))
        if principal is None:
            raise AppError("unauthorized", 401, "登录已失效")
        return principal

    async def login(self, username: str, password: str) -> Row:
        async with self.uow() as repos:
            row = await repos.identity.user_by_name(username)
        valid = await asyncio.to_thread(
            check_password, password, row["password_hash"] if row else self.dummy_hash
        )
        if not row or not valid or row["disabled"]:
            raise AppError("unauthorized", 401, "用户名或密码错误")
        token = secrets.token_urlsafe(32)
        async with self.uow() as repos:
            # 密码校验期间账号可能被停用；锁行后重查才允许签发。
            current = await repos.identity.user(row["id"], lock=True)
            if not current or current["disabled"] or current["password_hash"] != row["password_hash"]:
                raise AppError("unauthorized", 401, "用户名或密码错误")
            await repos.identity.save_token(digest_token(token), row["id"], 24)
        return {"token": token, "role": row["role"], "username": row["username"]}

    async def logout(self, token: str) -> None:
        async with self.uow() as repos:
            await repos.identity.delete_token(digest_token(token))

    async def create(
        self,
        principal: Principal | None,
        username: str,
        password: str,
        role: str = "user",
        usage_kind: str = "demo",
        *,
        bootstrap: bool = False,
    ) -> Row:
        if principal is not None:
            principal.require_admin()
        if role not in {"admin", "user"} or usage_kind not in {"demo", "real_usage"}:
            raise AppError("invalid_user", 400, "账号角色或用途无效")
        if not username.strip() or len(username) > 100 or not 12 <= len(password.encode()) <= 72:
            raise AppError("invalid_user", 400, "用户名不能为空，密码需为 12–72 字节")
        encoded = (await asyncio.to_thread(bcrypt.hashpw, password.encode(), bcrypt.gensalt())).decode()
        async with self.uow() as repos:
            await repos.access.revision(lock=True)
            if bootstrap:
                if principal is not None or role != "admin" or await repos.identity.users():
                    raise conflict("引导账号仅能在空账号库显式创建")
            elif principal is None:
                raise AppError("unauthorized", 401, "需要登录")
            else:
                await active(repos, principal, admin=True)
            if await repos.identity.user_by_name(username):
                raise conflict("用户名已存在")
            row = await repos.identity.create_user(
                {
                    "id": new_id(),
                    "username": username,
                    "password_hash": encoded,
                    "role": role,
                    "usage_kind": usage_kind,
                }
            )
            if principal:
                await repos.access.audit(principal.user_id, "user.create", row["id"], {"role": role})
        return row
