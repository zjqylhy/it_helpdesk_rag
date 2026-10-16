from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from psycopg_pool import AsyncConnectionPool

from helpdesk.domain.models import Row
from helpdesk.ports.repositories import Repositories

from .access import Access
from .catalog import Catalog
from .conversations import Conversations
from .identity import Identity


class PostgresUnitOfWork:
    def __init__(self, pool: AsyncConnectionPool[psycopg.AsyncConnection[Row]]) -> None:
        self.pool = pool

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[Repositories]:
        async with self.pool.connection() as conn:
            # 单个用例共享一条连接，异常退出统一回滚。
            yield Repositories(Identity(conn), Access(conn), Catalog(conn), Conversations(conn))
