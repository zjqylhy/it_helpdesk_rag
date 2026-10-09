import asyncio
import selectors
import sys

def loop_factory():
    # psycopg 异步连接不支持 Windows 默认 Proactor；显式使用兼容循环。
    if sys.platform == 'win32':
        return asyncio.SelectorEventLoop(selectors.SelectSelector())
    return asyncio.new_event_loop()
