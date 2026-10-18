import psycopg
from psycopg_pool import AsyncConnectionPool

from helpdesk.domain.errors import AppError
from helpdesk.domain.models import Principal, Row
from helpdesk.domain.rag import BoundScope

from .common import jsonb
from .conversations import Conversations


class ChatStore:
    def __init__(self, pool: AsyncConnectionPool[psycopg.AsyncConnection[Row]]) -> None:
        self.pool = pool

    async def prepare(
        self,
        principal: Principal,
        session_id: str,
        request_id: str,
        question: str,
        scope: BoundScope,
        strategy: str,
    ) -> tuple[Row, list[Row]]:
        async with self.pool.connection() as conn:
            repo = Conversations(conn)
            row, created = await repo.admit(session_id, request_id, question, principal)
            if not created:
                raise AppError("request_replayed", 409, "该请求已处理，请刷新会话历史")
            await conn.execute(
                "UPDATE messages SET provenance=%s,strategy_version=%s WHERE id=%s",
                (jsonb(self.provenance(scope)), strategy, row["id"]),
            )
            history = []
            for message in await repo.messages(session_id):
                if message["status"] in {"completed", "clarify"} and await repo.provenance_visible(
                    principal, message["provenance"]
                ):
                    history.extend(
                        [
                            {"role": "user", "content": message["question"]},
                            {"role": "assistant", "content": message["answer"]},
                        ]
                    )
            return row, history[-8:]

    def provenance(self, scope: BoundScope) -> Row:
        return {
            "kb_ids": list(scope.kb_ids),
            "version_ids": list(scope.version_ids),
            "snapshot_members": list(scope.snapshots),
            "authorization_revision": scope.revision,
            "scope_hash": scope.scope_hash,
        }

    async def finish(
        self, message_id: str, status: str, answer: str, citations: list[Row], trace: Row
    ) -> None:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    """UPDATE messages SET status=%s,answer=%s,citations=%s,trace=%s,
                completion_kind=%s WHERE id=%s AND status='generating' RETURNING id""",
                    (
                        status,
                        answer,
                        jsonb(citations),
                        jsonb(trace),
                        "full" if status == "completed" else None,
                        message_id,
                    ),
                )
            ).fetchone()
            if row is None:
                raise AppError("invalid_terminal", 409, "消息已结束")

    async def abort(self, message_id: str, status: str, reason: str) -> None:
        async with self.pool.connection() as conn:
            # 只用于尚未成功写出 done 的在途请求，覆盖提交成功而网络终态发送失败的窗口。
            await conn.execute(
                """UPDATE messages SET status=%s,answer='',citations='[]',trace=%s,completion_kind=NULL
                WHERE id=%s""",
                (status, jsonb({"stop_reason": reason}), message_id),
            )

    async def recover(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("""UPDATE messages SET status='interrupted',answer='',citations='[]',
                trace='{"stop_reason":"api_restarted"}',completion_kind=NULL WHERE status='generating' """)
            await conn.execute(
                """UPDATE model_attempts a SET status='unknown',finished_at=now()
                FROM model_runs r WHERE a.run_id=r.id AND a.status='started'
                AND r.message_id IS NOT NULL"""
            )
            # 问答运行与 Worker 分开；API 重启不能改写正在入库的运行。
            await conn.execute(
                "UPDATE model_runs SET status='interrupted' WHERE status='running' AND message_id IS NOT NULL"
            )
