import asyncio
from collections.abc import AsyncIterator
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from multiprocessing import get_context
from typing import Annotated
from urllib.parse import quote

import psycopg
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from helpdesk.adapters.files.local import LocalFiles
from helpdesk.adapters.models.client import model_client
from helpdesk.adapters.models.gateway import HttpModels
from helpdesk.adapters.postgres.chat import ChatStore
from helpdesk.adapters.postgres.corpus import PostgresCorpus
from helpdesk.adapters.postgres.migration import API_LOCK
from helpdesk.adapters.postgres.runs import RunLedger
from helpdesk.adapters.postgres.uow import PostgresUnitOfWork
from helpdesk.application.access import AccessService
from helpdesk.application.barrier import AuthorizationBarrier
from helpdesk.application.catalog import CatalogService
from helpdesk.application.chat import ChatService
from helpdesk.application.conversations import ConversationService
from helpdesk.application.identity import IdentityService, digest_token
from helpdesk.domain.contracts import ChatRequest
from helpdesk.domain.errors import AppError
from helpdesk.domain.models import Principal, Row
from helpdesk.ports.models import Models
from helpdesk.settings import Settings

from . import inputs
from .chat_stream import ChatResponse


@dataclass
class Services:
    uow: PostgresUnitOfWork
    barrier: AuthorizationBarrier
    identity: IdentityService
    access: AccessService
    catalog: CatalogService
    conversations: ConversationService
    chat: ChatService


class ResponseGuard:
    """普通响应在最终发送处复核授权；SSE 由逐事件 EventSink 单独处理。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        start: Message | None = None
        chunks: list[bytes] = []

        async def guarded(message: Message) -> None:
            nonlocal start
            state = scope.get("state", {})
            if scope["type"] != "http" or "auth_revision" not in state or state.get("owned_sse"):
                await send(message)
                return
            if message["type"] == "http.response.start":
                start = message
                return
            if message["type"] != "http.response.body" or start is None:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            services: Services = scope["app"].state.services
            async with services.barrier.read():
                if start["status"] == 204:
                    # 已鉴权用例的空成功响应不含资料；自停用或改密后仍确认操作已完成。
                    async with asyncio.timeout(5):
                        await send(start)
                        await send({"type": "http.response.body", "body": b""})
                    return
                async with services.uow() as repos:
                    principal = await repos.identity.principal(digest_token(state["token"]))
                    revision = await repos.access.revision()
                if principal is None:
                    response = JSONResponse({"error": "登录已失效", "code": "unauthorized"}, status_code=401)
                    await response(scope, receive, send)
                elif revision != state["auth_revision"]:
                    response = JSONResponse(
                        {"error": "权限已变化，请重试", "code": "authorization_changed"}, status_code=409
                    )
                    await response(scope, receive, send)
                else:
                    # 有界网络写出避免慢客户端长期阻塞撤权。
                    async with asyncio.timeout(5):
                        await send(start)
                        await send({"type": "http.response.body", "body": b"".join(chunks)})

        await self.app(scope, receive, guarded)


class UploadLimit:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or not scope.get("path", "").startswith("/api/v1/documents")
        ):
            await self.app(scope, receive, send)
            return
        limit = 20 * 1024 * 1024 + 64 * 1024
        headers = dict(scope.get("headers", []))
        length = headers.get(b"content-length", b"0")
        if not length.isdigit() or int(length) > limit:
            response = JSONResponse(
                {"error": "上传请求过大或长度无效", "code": "file_too_large"}, status_code=413
            )
            await response(scope, receive, send)
            return
        size = 0

        async def bounded() -> Message:
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > limit:
                raise HTTPException(413, "上传请求不能超过 20 MiB 与必要元数据")
            return message

        await self.app(scope, bounded, send)


def create_app(settings: Settings, models: Models | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool: AsyncConnectionPool[psycopg.AsyncConnection[Row]] = AsyncConnectionPool(
            settings.database_url.get_secret_value(),
            min_size=1,
            max_size=settings.pool_max,
            open=False,
            timeout=5,
            kwargs={"row_factory": dict_row, "connect_timeout": 5, "options": "-c statement_timeout=5000"},
        )
        client = model_client(settings)
        # Linux 同样用 spawn，避免子进程继承已启动的连接池线程和网络描述符。
        cpu = ProcessPoolExecutor(max_workers=2, mp_context=get_context("spawn"))
        lease = None
        try:
            await pool.open()
            await pool.wait(timeout=5)
            lease = await pool.getconn()
            # 同数据库第二 API 进程直接启动失败，不能绕过进程内撤权屏障。
            row = await (
                await lease.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (API_LOCK,))
            ).fetchone()
            await lease.commit()
            if not row or not row["acquired"]:
                raise RuntimeError("首版只允许一个 API 进程")
            check = await (
                await lease.execute("SELECT max(version) AS version FROM app_migrations")
            ).fetchone()
            await lease.commit()
            if not check or check["version"] != 3:
                raise RuntimeError("请先显式执行数据库迁移")
            uow = PostgresUnitOfWork(pool)
            barrier = AuthorizationBarrier()
            chat_store = ChatStore(pool)
            await chat_store.recover()
            provider = models or HttpModels(settings, client)
            app.state.services = Services(
                uow,
                barrier,
                IdentityService(uow),
                AccessService(uow, barrier),
                CatalogService(uow, LocalFiles(settings.upload_dir), barrier),
                ConversationService(uow),
                ChatService(
                    uow,
                    chat_store,
                    PostgresCorpus(pool, settings),
                    provider,
                    RunLedger(pool),
                    barrier,
                    settings,
                    cpu,
                ),
            )
            yield
        finally:
            if lease is not None:
                await lease.close()
                await pool.putconn(lease)
            await pool.close()
            await client.aclose()
            await asyncio.to_thread(cpu.shutdown, wait=True, cancel_futures=True)

    app = FastAPI(title="知答 Python API", version="0.2.0-m2", lifespan=lifespan)
    app.add_middleware(ResponseGuard)
    app.add_middleware(UploadLimit)
    bearer = HTTPBearer(auto_error=False)

    def services(request: Request) -> Services:
        return request.app.state.services

    async def principal(
        request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
    ) -> Principal:
        if credentials is None:
            raise AppError("unauthorized", 401, "需要登录")
        svc = services(request)
        async with svc.barrier.read():
            result = await svc.identity.authenticate(credentials.credentials)
            async with svc.uow() as repos:
                request.state.auth_revision = await repos.access.revision()
            request.state.token = credentials.credentials
        return result

    # 闭包依赖不能写成尚未解析的局部字符串注解。
    User = Annotated[Principal, Depends(principal)]

    @app.exception_handler(AppError)
    async def app_error(request: Request, error: AppError) -> JSONResponse:
        return JSONResponse({"error": error.message, "code": error.code}, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": "请求字段格式无效", "code": "invalid_request"}, status_code=400)

    @app.exception_handler(psycopg.IntegrityError)
    async def integrity_error(request: Request, error: psycopg.IntegrityError) -> JSONResponse:
        return JSONResponse({"error": "数据冲突或引用资源无效", "code": "conflict"}, status_code=409)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        return JSONResponse({"error": str(error.detail), "code": "http_error"}, status_code=error.status_code)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception) -> JSONResponse:
        return JSONResponse({"error": "服务暂时不可用", "code": "internal_error"}, status_code=500)

    @app.get("/health")
    async def health(request: Request) -> Row:
        async with services(request).uow() as repos:
            await repos.access.revision()
        return {"status": "ok", "stage": "m2", "chat_ready": models is not None or settings.models_ready}

    @app.post("/api/v1/auth/login")
    async def login(body: inputs.Login, request: Request) -> Row:
        return await services(request).identity.login(body.username, body.password)

    @app.post("/api/v1/auth/logout")
    async def logout(request: Request, user: User) -> Response:
        await services(request).identity.logout(request.state.token)
        # 本请求主动注销，返回空正文，不再尝试用旧 Token 复核。
        del request.state.auth_revision
        return Response(status_code=204)

    @app.get("/api/v1/auth/me")
    async def me(user: User) -> Row:
        return {"ID": user.user_id, "Role": user.role}

    @app.get("/api/v1/knowledge-bases")
    async def knowledge_bases(request: Request, user: User) -> list[Row]:
        return await services(request).catalog.knowledge_bases(user)

    @app.post("/api/v1/knowledge-bases", status_code=201)
    async def create_kb(body: inputs.KnowledgeBase, request: Request, user: User) -> Row:
        row = await services(request).catalog.create_kb(user, body.name, body.organization)
        return {"id": row["id"]}

    @app.patch("/api/v1/knowledge-bases/{kb_id}/default-search", status_code=204)
    async def default_search(kb_id: str, body: inputs.Enabled, request: Request, user: User) -> Response:
        await services(request).catalog.default_search(user, kb_id, body.enabled)
        return Response(status_code=204)

    @app.get("/api/v1/documents")
    async def documents(request: Request, user: User, kb_id: str | None = None) -> list[Row]:
        return await services(request).catalog.documents(user, kb_id)

    async def save_upload(
        request: Request,
        user: Principal,
        file: UploadFile,
        kb_id: str,
        document_id: str | None,
        title: str,
        source: str,
        confirm: bool,
    ) -> Row:
        async def parts() -> AsyncIterator[bytes]:
            while chunk := await file.read(64 * 1024):
                yield chunk

        try:
            return await services(request).catalog.upload(
                user, kb_id, document_id, file.filename or "", title, source, confirm, parts()
            )
        finally:
            await file.close()

    @app.post("/api/v1/documents", status_code=202)
    async def upload(
        request: Request,
        user: User,
        file: Annotated[UploadFile, File()],
        kb_id: Annotated[str, Form()],
        title: Annotated[str, Form()],
        source: Annotated[str, Form()] = "",
        confirm_duplicate: Annotated[bool, Form()] = False,
    ) -> Row:
        row = await save_upload(request, user, file, kb_id, None, title, source, confirm_duplicate)
        return {k: row[k] for k in ("document_id", "version_id", "job_id")}

    @app.post("/api/v1/documents/{document_id}/versions", status_code=202)
    async def upload_version(
        document_id: str,
        request: Request,
        user: User,
        file: Annotated[UploadFile, File()],
        title: Annotated[str, Form()] = "追加版本",
        source: Annotated[str, Form()] = "",
        confirm_duplicate: Annotated[bool, Form()] = False,
    ) -> Row:
        row = await save_upload(request, user, file, "", document_id, title, source, confirm_duplicate)
        return {k: row[k] for k in ("document_id", "version_id", "job_id")}

    @app.get("/api/v1/documents/{document_id}/versions")
    async def versions(document_id: str, request: Request, user: User) -> list[Row]:
        return await services(request).catalog.versions(user, document_id)

    @app.get("/api/v1/documents/{document_id}/versions/{version_id}/chunks")
    async def chunks(document_id: str, version_id: str, request: Request, user: User) -> list[Row]:
        return await services(request).catalog.chunks(user, document_id, version_id)

    @app.patch("/api/v1/documents/{document_id}/enabled", status_code=204)
    async def enabled(document_id: str, body: inputs.Enabled, request: Request, user: User) -> Response:
        result = await services(request).access.change(
            user, "document.enabled", document_id, body.model_dump()
        )
        request.state.auth_revision = result["authorization_revision"]
        return Response(status_code=204)

    @app.post("/api/v1/ingestion-jobs/{job_id}/retry", status_code=202)
    async def retry(job_id: str, request: Request, user: User) -> Response:
        await services(request).catalog.retry(user, job_id)
        return Response(status_code=202)

    @app.get("/api/v1/files/{version_id}")
    async def download(version_id: str, request: Request, user: User) -> Response:
        content, filename = await services(request).catalog.download(user, version_id)
        return Response(
            content,
            media_type="application/octet-stream",
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename, safe="")},
        )

    @app.get("/api/v1/sessions")
    async def sessions(request: Request, user: User) -> list[Row]:
        return await services(request).conversations.sessions(user)

    @app.post("/api/v1/sessions", status_code=201)
    async def create_session(body: inputs.SessionCreate, request: Request, user: User) -> Row:
        if body.kb_id and body.kb_ids and body.kb_ids != [body.kb_id]:
            raise AppError("invalid_scope", 400, "知识库范围字段冲突")
        row = await services(request).conversations.create(
            user, body.kb_ids or ([body.kb_id] if body.kb_id else [])
        )
        return {"id": row["id"]}

    @app.get("/api/v1/sessions/{session_id}/messages")
    async def messages(session_id: str, request: Request, user: User) -> list[Row]:
        return await services(request).conversations.history(user, session_id)

    @app.post(
        "/api/v1/chat",
        response_class=Response,
        responses={
            200: {
                "description": "有序 SSE 业务事件",
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
            },
            503: {"description": "模型尚未配置"},
        },
    )
    async def chat(body: ChatRequest, request: Request, user: User) -> Response:
        if models is None and not settings.models_ready:
            raise AppError("models_not_configured", 503, "模型服务尚未配置")
        prepared = await services(request).chat.prepare(
            user, request.state.token, body.session_id, body.request_id, body.question
        )
        request.state.owned_sse = True
        return ChatResponse(services(request).chat, prepared)

    @app.get("/api/v1/admin/users")
    async def users(request: Request, user: User) -> list[Row]:
        return await services(request).access.list(user, "users")

    @app.post("/api/v1/admin/users", status_code=204)
    async def create_user(body: inputs.UserCreate, request: Request, user: User) -> Response:
        svc = services(request)
        async with svc.barrier.write():
            await svc.identity.create(user, body.username, body.password, body.role, body.usage_kind)
            async with svc.uow() as repos:
                revision = await repos.access.revision()
            svc.barrier.revoke(revision)
            request.state.auth_revision = revision
        return Response(status_code=204)

    async def access_change(
        request: Request, user: Principal, action: str, target: str, details: Row
    ) -> Response:
        result = await services(request).access.change(user, action, target, details)
        request.state.auth_revision = result["authorization_revision"]
        return Response(status_code=204)

    @app.patch("/api/v1/admin/users/{user_id}/disabled", status_code=204)
    async def disabled(user_id: str, body: inputs.Disabled, request: Request, user: User) -> Response:
        return await access_change(request, user, "user.disabled", user_id, body.model_dump())

    @app.patch("/api/v1/admin/users/{user_id}", status_code=204)
    async def update_user(user_id: str, body: inputs.UserUpdate, request: Request, user: User) -> Response:
        request.state.auth_revision = await services(request).access.update_user(
            user, user_id, body.disabled, body.usage_kind, body.password
        )
        return Response(status_code=204)

    @app.get("/api/v1/admin/groups")
    async def groups(request: Request, user: User) -> list[Row]:
        return await services(request).access.list(user, "groups")

    @app.post("/api/v1/admin/groups", status_code=204)
    async def create_group(body: inputs.Group, request: Request, user: User) -> Response:
        return await access_change(request, user, "group.create", "", body.model_dump())

    @app.put("/api/v1/admin/groups/{group_id}/members", status_code=204)
    async def members(group_id: str, body: inputs.Members, request: Request, user: User) -> Response:
        return await access_change(request, user, "group.members", group_id, body.model_dump())

    @app.get("/api/v1/admin/documents/{document_id}/access")
    async def document_access(document_id: str, request: Request, user: User) -> list[Row]:
        return [await services(request).access.document_access(user, document_id)]

    @app.put("/api/v1/admin/documents/{document_id}/access", status_code=204)
    async def grant(document_id: str, body: inputs.DocumentAccess, request: Request, user: User) -> Response:
        return await access_change(request, user, "document.access", document_id, body.model_dump())

    @app.get("/api/v1/admin/audit-events")
    async def audit(request: Request, user: User) -> list[Row]:
        return await services(request).access.list(user, "audit")

    return app
