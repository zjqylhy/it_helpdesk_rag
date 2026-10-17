"""服务器离线评测准备：显式管理授权、普通主体登录、来源审核包，零模型调用。"""

import argparse
import json
import os
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from helpdesk.adapters.postgres.corpus import PostgresCorpus
from helpdesk.application.rebinding import prepare_rebinding, unique_mapping
from helpdesk.application.releases import canonical_hash, read_release
from helpdesk.domain.models import Principal, Row
from helpdesk.domain.rag import BoundScope
from helpdesk.entrypoints.evaluate import dependencies, identity
from helpdesk.entrypoints.runtime import run
from helpdesk.settings import Settings


def private_write(path: Path, value: Row) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    with os.fdopen(
        os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8"
    ) as f:
        json.dump(value, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


async def collect(
    corpus: PostgresCorpus, principal: Principal, revision: int
) -> tuple[BoundScope, list[Row]]:
    scope = await corpus.bind(principal, (), revision)
    rows = await corpus.review_export(principal, scope)
    return scope, [{**r, "knowledge_base_name": r["kb_name"]} for r in rows]


async def provision(args: argparse.Namespace, settings: Settings) -> None:
    _, original, _ = read_release(args.source)
    parsed = urlsplit(args.api_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("管理接口地址无效")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("明文管理接口仅允许本机回环")
    async with dependencies(settings) as (_, corpus, uow):
        principal, revision = await identity(uow)
        principal.require_admin()
        scope, rows = await collect(corpus, principal, revision)
        mapping = unique_mapping(original, scope, rows)
    if not args.apply:
        print(json.dumps({"status": "planned", "documents": len(mapping["document_ids"]), "model_calls": 0}))
        return
    if args.credentials.exists():
        state = json.loads(args.credentials.read_text(encoding="utf-8"))
        if state["source_scope_version"] != original["scope_version"] or state["username"] != args.username:
            raise ValueError("私有凭据文件不属于本批来源或账号")
    else:
        state = {
            "status": "preparing",
            "username": args.username,
            "password": secrets.token_urlsafe(30),
            "group_name": args.group_name,
            "source_scope_version": original["scope_version"],
        }
        private_write(args.credentials, state)
    if state["group_name"] != args.group_name:
        raise ValueError("专用用户组名称发生变化")
    headers = {"Authorization": "Bearer " + os.environ["HD_EVAL_TOKEN"]}
    async with httpx.AsyncClient(
        base_url=args.api_url, timeout=15, trust_env=False, follow_redirects=False
    ) as api:

        async def request(method: str, path: str, body: Row | None = None) -> Any:
            response = await api.request(method, path, headers=headers, json=body)
            if not response.is_success:
                raise RuntimeError(f"评测主体准备接口失败：{response.status_code} {path}")
            return response.json() if response.content else None

        users = await request("GET", "/api/v1/admin/users")
        user = next((u for u in users if u["username"] == args.username), None)
        if user is None:
            await request(
                "POST",
                "/api/v1/admin/users",
                {
                    "username": state["username"],
                    "password": state["password"],
                    "role": "user",
                    "usage_kind": "demo",
                },
            )
            users = await request("GET", "/api/v1/admin/users")
            user = next(u for u in users if u["username"] == args.username)
        if user["role"] != "user" or user["disabled"]:
            raise ValueError("目标账号不是启用的普通评测用户")
        login = await api.post("/api/v1/auth/login", json={k: state[k] for k in ("username", "password")})
        if not login.is_success:
            raise ValueError("无法使用本批私有凭据登录；不会重置已有账号密码")
        previous_token = state.get("token")
        state.update(user_id=user["id"], token=login.json()["token"])
        private_write(args.credentials, state)
        if previous_token and previous_token != state["token"]:
            response = await api.post(
                "/api/v1/auth/logout", headers={"Authorization": "Bearer " + previous_token}
            )
            if response.status_code not in {204, 401}:
                raise ValueError("旧评测会话令牌清理失败")
        groups = await request("GET", "/api/v1/admin/groups")
        group = next((g for g in groups if g["name"] == args.group_name), None)
        if group is not None and state.get("group_id") != group["id"]:
            raise ValueError("同名用户组未绑定本批凭据，需人工核对创建状态")
        if group is None:
            await request("POST", "/api/v1/admin/groups", {"name": args.group_name})
            groups = await request("GET", "/api/v1/admin/groups")
            group = next(g for g in groups if g["name"] == args.group_name)
        if state.get("group_id") not in {None, group["id"]}:
            raise ValueError("专用用户组标识发生变化")
        if any(uid != user["id"] for uid in group["user_ids"]):
            raise ValueError("专用用户组已有其他成员，拒绝覆盖")
        state["group_id"] = group["id"]
        private_write(args.credentials, state)
        if group["user_ids"] != [user["id"]]:
            await request(
                "PUT", "/api/v1/admin/groups/" + group["id"] + "/members", {"user_ids": [user["id"]]}
            )
        for doc_id in mapping["document_ids"]:
            path = "/api/v1/admin/documents/" + doc_id + "/access"
            records = await request("GET", path)
            if not isinstance(records, list) or len(records) != 1:
                raise ValueError("文档授权接口应返回单条记录数组")
            access = records[0]
            if access["visibility"] == "groups" and group["id"] not in access["group_ids"]:
                await request(
                    "PUT", path, {"visibility": "groups", "group_ids": [*access["group_ids"], group["id"]]}
                )
        user_headers = {"Authorization": "Bearer " + state["token"]}
        response = await api.get("/api/v1/admin/users", headers=user_headers)
        if response.status_code != 403:
            raise ValueError("普通评测账号的管理边界验证失败")
    previous = os.environ["HD_EVAL_TOKEN"]
    os.environ["HD_EVAL_TOKEN"] = state["token"]
    try:
        async with dependencies(settings) as (_, corpus, uow):
            principal, revision = await identity(uow)
            scope, rows = await collect(corpus, principal, revision)
            unique_mapping(original, scope, rows)
            async with uow() as repos:
                visible = await repos.catalog.knowledge_bases(principal)
            if {k["id"] for k in visible} != set(scope.kb_ids):
                raise ValueError("评测账号可见额外知识库")
    finally:
        os.environ["HD_EVAL_TOKEN"] = previous
    state.update(status="ready", authorization_revision=revision, scope_hash=scope.scope_hash)
    private_write(args.credentials, state)
    print(
        json.dumps(
            {
                "status": "ready",
                "role": "user",
                "knowledge_bases": len(scope.kb_ids),
                "versions": len(scope.version_ids),
                "chunks": len(rows),
                "revision": revision,
                "model_calls": 0,
            }
        )
    )


async def prepare(args: argparse.Namespace, settings: Settings) -> None:
    async with dependencies(settings) as (pool, corpus, uow):
        principal, revision = await identity(uow)
        if principal.role != "user":
            raise ValueError("来源审核必须使用普通评测主体")
        scope, rows = await collect(corpus, principal, revision)
        async with uow() as repos:
            visible = await repos.catalog.knowledge_bases(principal)
        if {k["id"] for k in visible} != set(scope.kb_ids):
            raise ValueError("评测账号可见额外知识库")
        again, repeated = await collect(corpus, principal, revision)
        current, current_revision = await identity(uow)
        if (
            current != principal
            or current_revision != revision
            or again != scope
            or canonical_hash(rows) != canonical_hash(repeated)
        ):
            raise ValueError("来源导出期间权限或语料发生变化")
        async with pool.connection() as conn:
            groups = await conn.execute(
                "SELECT group_id FROM user_group_members WHERE user_id=%s", (principal.user_id,)
            )
            group_ids = [r["group_id"] for r in await groups.fetchall()]
        packet = prepare_rebinding(
            args.source,
            args.out,
            scope,
            rows,
            {
                "user_id": principal.user_id,
                "revision": revision,
                "role": principal.role,
                "group_ids": sorted(group_ids),
            },
        )
    print(json.dumps({k: packet[k] for k in ("status", "packet_hash", "cases", "chunks", "model_calls")}))


def main() -> None:
    parser = argparse.ArgumentParser(description="服务器主体与来源准备；不执行模型调用")
    commands = parser.add_subparsers(dest="command", required=True)
    account = commands.add_parser("provision")
    account.add_argument("source", type=Path)
    account.add_argument("--api-url", default="http://127.0.0.1:18080")
    account.add_argument("--username", required=True)
    account.add_argument("--group-name", required=True)
    account.add_argument("--credentials", type=Path, required=True)
    account.add_argument("--apply", action="store_true")
    packet = commands.add_parser("prepare")
    packet.add_argument("source", type=Path)
    packet.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    run((provision if args.command == "provision" else prepare)(args, Settings()))  # type: ignore[call-arg]


if __name__ == "__main__":
    main()
