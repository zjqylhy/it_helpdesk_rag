import asyncio
from collections.abc import AsyncIterator
from pathlib import PurePosixPath

from helpdesk.application.barrier import AuthorizationBarrier
from helpdesk.application.identity import active
from helpdesk.domain.errors import AppError
from helpdesk.domain.models import Principal, Row
from helpdesk.ports.repositories import FileStore, UnitOfWork

VERSION_PUBLIC = {
    "id",
    "filename",
    "source",
    "status",
    "error",
    "embedding_model",
    "dimension",
    "created_at",
    "job_id",
    "progress",
}


class CatalogService:
    def __init__(self, uow: UnitOfWork, files: FileStore, barrier: AuthorizationBarrier) -> None:
        self.uow, self.files, self.barrier = uow, files, barrier

    async def knowledge_bases(self, principal: Principal) -> list[Row]:
        async with self.uow() as repos:
            return await repos.catalog.knowledge_bases(await active(repos, principal))

    async def create_kb(self, principal: Principal, name: str, organization: str) -> Row:
        async with self.uow() as repos:
            await active(repos, principal, admin=True)
            return await repos.catalog.create_kb(name, organization)

    async def default_search(self, principal: Principal, kb_id: str, enabled: bool) -> Row:
        async with self.uow() as repos:
            await active(repos, principal, admin=True)
            return await repos.catalog.default_search(kb_id, enabled)

    async def documents(self, principal: Principal, kb_id: str | None) -> list[Row]:
        async with self.uow() as repos:
            return await repos.catalog.documents(await active(repos, principal), kb_id)

    async def versions(self, principal: Principal, document_id: str) -> list[Row]:
        async with self.uow() as repos:
            current = await active(repos, principal)
            await repos.catalog.document(current, document_id)
            return [
                {k: v for k, v in row.items() if k in VERSION_PUBLIC}
                for row in await repos.catalog.versions(document_id)
            ]

    async def upload(
        self,
        principal: Principal,
        kb_id: str,
        document_id: str | None,
        filename: str,
        title: str,
        source: str,
        confirm_duplicate: bool,
        parts: AsyncIterator[bytes],
    ) -> Row:
        # 落盘前完成权限校验，落盘后再校验；长上传不占用数据库事务或撤权写屏障。
        async with self.uow() as repos:
            await active(repos, principal, admin=True)
        filename = filename.replace("\\", "/").split("/")[-1]
        suffix = PurePosixPath(filename).suffix.lower()
        if len(filename) > 255 or not title.strip() or len(title) > 500 or len(source) > 2000:
            raise AppError("invalid_document", 400, "文档元数据无效")
        key, digest, _ = await self.files.save(parts, suffix)
        try:
            async with self.barrier.read(), self.uow() as repos:
                await active(repos, principal, admin=True)
                if document_id:
                    document = await repos.catalog.document(principal, document_id)
                    kb_id = document["kb_id"]
                duplicate = await repos.catalog.duplicate(kb_id, digest)
                if document_id:
                    await repos.catalog.document(principal, document_id, lock=True)
                if duplicate and not confirm_duplicate:
                    raise AppError("duplicate_content", 409, "当前知识库已有相同内容，请确认后上传")
                return await repos.catalog.queue_document(
                    kb_id,
                    document_id,
                    {
                        "filename": filename,
                        "title": title.strip(),
                        "source": source,
                        "storage_key": key,
                        "content_hash": digest,
                    },
                )
        except BaseException:
            # 提交确认丢失时不能误删已被版本引用的文件；数据库不可用时保守保留孤儿待核对。
            async def cleanup() -> None:
                try:
                    async with self.uow() as check:
                        used = await check.catalog.storage_key_in_use(key)
                except Exception:
                    return
                if not used:
                    await self.files.remove(key)

            await asyncio.shield(cleanup())
            raise

    async def download(self, principal: Principal, version_id: str) -> tuple[bytes, str]:
        async with self.uow() as repos:
            row = await repos.catalog.version(await active(repos, principal), version_id)
        if not row["storage_key"]:
            raise AppError("storage_mapping_required", 409, "历史文件需先完成安全映射")
        data = await self.files.read(row["storage_key"], row["content_hash"])
        async with self.uow() as repos:
            await repos.catalog.version(await active(repos, principal), version_id)
        return data, row["filename"]

    async def chunks(self, principal: Principal, document_id: str, version_id: str) -> list[Row]:
        async with self.uow() as repos:
            await repos.catalog.document(await active(repos, principal), document_id)
            return await repos.catalog.chunks(document_id, version_id)

    async def retry(self, principal: Principal, job_id: str) -> Row:
        async with self.uow() as repos:
            await active(repos, principal, admin=True)
            row = await repos.catalog.retry(job_id)
            return {
                k: v
                for k, v in row.items()
                if k in {"id", "version_id", "status", "progress", "attempts", "updated_at"}
            }
