import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from helpdesk.domain.errors import AppError


class AuthorizationBarrier:
    """首版单 API 进程的撤权屏障；写者优先，避免持续输出饿死撤权。"""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting = 0
        self._runs: dict[str, tuple[Callable[[], Awaitable[None]], asyncio.Task[object]]] = {}

    @asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        async with self._condition:
            await self._condition.wait_for(lambda: not self._writer and self._waiting == 0)
            self._readers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._readers -= 1
                self._condition.notify_all()

    @asynccontextmanager
    async def write(self) -> AsyncIterator[None]:
        async with self._condition:
            self._waiting += 1
            try:
                await self._condition.wait_for(lambda: not self._writer and self._readers == 0)
                self._writer = True
            finally:
                self._waiting -= 1
                self._condition.notify_all()
        try:
            yield
        finally:
            async with self._condition:
                self._writer = False
                self._condition.notify_all()

    def register(
        self, request_id: str, check: Callable[[], Awaitable[None]], task: asyncio.Task[object]
    ) -> None:
        # 调用者必须在准入读屏障中完成数据库复核和注册，不能只先注册后检查。
        if request_id in self._runs:
            raise AppError("request_active", 409, "请求正在执行")
        self._runs[request_id] = (check, task)

    def unregister(self, request_id: str) -> None:
        self._runs.pop(request_id, None)

    async def recheck(self) -> None:
        # 调用者持有写屏障；复核回调不得再次申请读屏障。
        for check, task in tuple(self._runs.values()):
            if task.done() or task.cancelling():
                continue
            try:
                async with asyncio.timeout(6):
                    await check()
            except Exception as error:
                code = (
                    "authorization_changed"
                    if isinstance(error, AppError) and error.code == "authorization_changed"
                    else "authorization_check_failed"
                )
                task.cancel(code)
