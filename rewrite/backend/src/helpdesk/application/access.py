import asyncio

import bcrypt

from helpdesk.application.barrier import AuthorizationBarrier
from helpdesk.application.identity import active
from helpdesk.domain.errors import AppError
from helpdesk.domain.models import Principal, Row
from helpdesk.ports.repositories import UnitOfWork


class AccessService:
    async def update_user(
        self,
        principal: Principal,
        user_id: str,
        disabled: bool | None,
        usage_kind: str | None,
        password: str | None,
    ) -> int:
        principal.require_admin()
        if disabled is None and usage_kind is None and password is None:
            raise AppError("invalid_user", 400, "缺少账号更新字段")
        if password is not None and not 12 <= len(password.encode()) <= 72:
            raise AppError("invalid_user", 400, "密码需为 12–72 字节")
        encoded = (
            (await asyncio.to_thread(bcrypt.hashpw, password.encode(), bcrypt.gensalt())).decode()
            if password
            else None
        )
        async with self.barrier.write():
            async with self.uow() as repos:
                await repos.access.revision(lock=True)
                await active(repos, principal, admin=True)
                if disabled is not None:
                    await repos.access.disable_user(user_id, disabled)
                await repos.access.update_user(user_id, usage_kind, encoded)
                revision = await repos.access.audit(
                    principal.user_id,
                    "user.update",
                    user_id,
                    {
                        "disabled": disabled,
                        "usage_kind": usage_kind,
                        "password_changed": password is not None,
                    },
                )
            self.barrier.revoke(revision)
        return revision

    def __init__(self, uow: UnitOfWork, barrier: AuthorizationBarrier) -> None:
        self.uow, self.barrier = uow, barrier

    async def change(self, principal: Principal, action: str, target: str, details: Row) -> Row:
        async with self.barrier.write():
            async with self.uow() as repos:
                await repos.access.revision(lock=True)
                await active(repos, principal, admin=True)
                result: Row = {}
                if action == "group.create":
                    result = await repos.access.create_group(details["name"])
                    target = result["id"]
                elif action == "group.members":
                    await repos.access.set_members(target, list(dict.fromkeys(details["user_ids"])))
                elif action == "document.access":
                    if details["visibility"] not in {"all_users", "groups"}:
                        raise AppError("invalid_visibility", 400, "文档可见性无效")
                    groups = list(dict.fromkeys(details["group_ids"]))
                    if details["visibility"] == "all_users" and groups:
                        raise AppError("invalid_grants", 400, "全员文档不能同时配置用户组")
                    await repos.access.set_document_access(target, details["visibility"], groups)
                elif action == "user.disabled":
                    await repos.access.disable_user(target, details["disabled"])
                elif action == "document.enabled":
                    result = await repos.catalog.set_enabled(target, details["enabled"])
                else:
                    raise AppError("invalid_action", 400, "不支持的权限操作")
                revision = await repos.access.audit(principal.user_id, action, target, details)
            # 退出事务即提交；随后仍在写屏障内撤销旧运行，最后才响应成功。
            self.barrier.revoke(revision)
            return {**result, "authorization_revision": revision}

    async def list(self, principal: Principal, kind: str) -> list[Row]:
        async with self.uow() as repos:
            await active(repos, principal, admin=True)
            if kind == "users":
                return await repos.identity.users()
            if kind == "groups":
                return await repos.access.groups()
            return await repos.access.events()

    async def document_access(self, principal: Principal, document_id: str) -> Row:
        async with self.uow() as repos:
            await active(repos, principal, admin=True)
            return await repos.access.document_access(document_id)
