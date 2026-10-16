import argparse
import os
from pathlib import Path

from helpdesk.adapters.postgres.migration import migrate


def main() -> None:
    parser = argparse.ArgumentParser(description="显式事务迁移，不自动连接旧业务库")
    parser.add_argument("--directory", type=Path, default=Path("db/migrations"))
    args = parser.parse_args()
    dsn = os.environ.get("HD_DATABASE_URL")
    if not dsn:
        parser.error("必须设置 HD_DATABASE_URL")
    versions = migrate(dsn, args.directory)
    print("迁移完成：" + (",".join(map(str, versions)) if versions else "无需更新"))


if __name__ == "__main__":
    main()
