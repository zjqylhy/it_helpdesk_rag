import asyncio
import sys
from collections.abc import Coroutine
from typing import Any


def run[T](operation: Coroutine[Any, Any, T]) -> T:
    # psycopg 在 Windows 不支持 Proactor；显式指定，避免服务器覆盖全局事件循环策略。
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else asyncio.new_event_loop
    with asyncio.Runner(loop_factory=factory) as runner:
        return runner.run(operation)
