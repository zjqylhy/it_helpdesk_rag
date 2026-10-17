import psycopg
from psycopg_pool import AsyncConnectionPool

from helpdesk.domain.models import Row, new_id
from helpdesk.domain.rag import Usage

from .common import jsonb


class RunLedger:
    def __init__(self, pool: AsyncConnectionPool[psycopg.AsyncConnection[Row]]) -> None:
        self.pool = pool

    async def start_run(self, message_id: str | None, manifest: Row) -> str:
        run_id = new_id()
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO model_runs(id,message_id,manifest,status) VALUES(%s,%s,%s,'running')",
                (run_id, message_id, jsonb(manifest)),
            )
        return run_id

    async def start_attempt(self, run_id: str, stage: str, reserved: int) -> str:
        attempt_id = new_id()
        async with self.pool.connection() as conn:
            await conn.execute(
                """INSERT INTO model_attempts(id,run_id,stage,reserved_tokens,status)
                VALUES(%s,%s,%s,%s,'started')""",
                (attempt_id, run_id, stage, reserved),
            )
        return attempt_id

    async def end_attempt(self, attempt_id: str, status: str, usage: Usage) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """UPDATE model_attempts SET status=%s,input_tokens=%s,output_tokens=%s,finished_at=now()
                WHERE id=%s AND status='started' """,
                (status, usage.input_tokens, usage.output_tokens, attempt_id),
            )

    async def finish_run(self, run_id: str, status: str, trace: Row) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE model_runs SET status=%s,manifest=manifest || %s WHERE id=%s",
                (status, jsonb({"result": trace}), run_id),
            )
