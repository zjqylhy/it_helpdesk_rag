import argparse
import asyncio
import getpass
import os
import sys

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from helpdesk.adapters.postgres.uow import PostgresUnitOfWork
from helpdesk.application.identity import IdentityService
from helpdesk.domain.models import Row


async def bootstrap(dsn: str, username: str, password: str) -> None:
    pool: AsyncConnectionPool[psycopg.AsyncConnection[Row]] = AsyncConnectionPool(
        dsn, open=False, kwargs={"row_factory": dict_row}
    )
    async with pool:
        result = await IdentityService(PostgresUnitOfWork(pool)).create(
            None, username, password, role="admin", bootstrap=True
        )
        print("引导管理员已创建：" + result["username"])


def main() -> None:
    parser = argparse.ArgumentParser(description="仅在空账号库显式创建引导管理员，不重置已有密码")
    parser.add_argument("username")
    args = parser.parse_args()
    dsn = os.environ.get("HD_DATABASE_URL")
    if not dsn:
        parser.error("必须设置 HD_DATABASE_URL")
    password = getpass.getpass("管理员密码：")
    if password != getpass.getpass("再次输入："):
        parser.error("两次密码不一致")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(bootstrap(dsn, args.username, password))


if __name__ == "__main__":
    main()
