"""只读诊断 Python 应用配置和 schema，不执行迁移或模型请求。"""

import sys

import psycopg
from pydantic import ValidationError

from helpdesk.settings import Settings


def main() -> int:
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError:
        print("配置无效：请检查 .env.python 中的 HD_DATABASE_URL、HD_UPLOAD_DIR 和数值范围。")
        return 1
    if settings.model_ca_file and not settings.model_ca_file.is_file():
        print("模型 CA 文件不存在。")
        return 1
    try:
        with psycopg.connect(
            settings.database_url.get_secret_value(),
            connect_timeout=5,
            options="-c statement_timeout=5000",
        ) as conn:
            vector = conn.execute("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector')").fetchone()
            version = conn.execute("SELECT max(version) FROM app_migrations").fetchone()
    except psycopg.Error:
        print("数据库未就绪：检查连接、pgvector，并在停止 API 后显式执行 Migrate。")
        return 1
    if not vector or not vector[0] or not version or version[0] != 3:
        print("数据库版本或 pgvector 未就绪，请显式执行 Migrate。")
        return 1
    print("PostgreSQL、pgvector 与 Python schema 就绪。")
    print("模型：" + ("已配置（未发送探测请求）。" if settings.models_ready else "未启用或配置未完整。"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
