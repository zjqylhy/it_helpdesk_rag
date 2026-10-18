import asyncio
import hashlib
import re
from collections.abc import AsyncIterator, Callable
from functools import partial
from pathlib import Path
from uuid import uuid4

from helpdesk.domain.errors import AppError

MAX_UPLOAD = 20 * 1024 * 1024
SUFFIXES = {".pdf", ".docx", ".md", ".txt"}


async def complete_io[T](operation: Callable[[], T]) -> T:
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # 不让已启动的写线程在文件关闭或清理后继续写入。
        await task
        raise


class LocalFiles:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def path(self, key: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}\.(pdf|docx|md|txt)", key):
            raise AppError("invalid_storage_key", 409, "原文件尚未完成安全映射")
        target = (self.root / key).resolve()
        if not target.is_relative_to(self.root):
            raise AppError("invalid_storage_key", 409, "原文件位置无效")
        return target

    async def save(self, parts: AsyncIterator[bytes], suffix: str) -> tuple[str, str, int]:
        if suffix not in SUFFIXES:
            raise AppError("unsupported_file", 400, "仅支持 PDF、DOCX、MD 和 TXT")
        key = uuid4().hex + suffix
        path = self.path(key)
        await asyncio.to_thread(self.root.mkdir, parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")
        size = 0
        digest = hashlib.sha256()
        try:
            with temporary.open("xb") as stream:
                async for chunk in parts:
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise AppError("file_too_large", 413, "文件不能超过 20 MiB")
                    digest.update(chunk)
                    await complete_io(partial(stream.write, chunk))
            if not size:
                raise AppError("empty_file", 400, "不能上传空文件")
            await complete_io(partial(temporary.replace, path))
            return key, digest.hexdigest(), size
        except BaseException:
            # 即使请求取消也清理半成品；已入库的文件由用例负责归属。
            temporary.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            raise

    async def remove(self, key: str) -> None:
        await asyncio.to_thread(self.path(key).unlink, missing_ok=True)

    async def read(self, key: str, expected_hash: str) -> bytes:
        try:
            if (await asyncio.to_thread(self.path(key).stat)).st_size > MAX_UPLOAD:
                raise AppError("file_integrity", 409, "原文件大小超出允许范围")
            data = await asyncio.to_thread(self.path(key).read_bytes)
        except FileNotFoundError as error:
            raise AppError("file_missing", 409, "原文件不可用，请联系管理员") from error
        if hashlib.sha256(data).hexdigest() != expected_hash:
            raise AppError("file_integrity", 409, "原文件完整性检查失败")
        return data
