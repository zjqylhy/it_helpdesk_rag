import asyncio
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

from helpdesk.application.barrier import AuthorizationBarrier
from helpdesk.application.identity import digest_token
from helpdesk.application.model_session import Budget, ModelSession
from helpdesk.domain.errors import AppError
from helpdesk.domain.models import Principal, Row
from helpdesk.domain.rag import BoundScope
from helpdesk.graph.baseline import PROMPT_HASHES, Context, GraphRunner
from helpdesk.ports.chat import ChatPersistence, ChatRunInput
from helpdesk.ports.corpus import AuthorizedCorpus
from helpdesk.ports.models import Models, RunJournal
from helpdesk.ports.repositories import UnitOfWork
from helpdesk.settings import Settings


@dataclass
class Prepared:
    run: ChatRunInput
    scope: BoundScope
    token: str
    run_id: str
    started: float
    first_progress_ms: float | None = None
    server_ttft_ms: float | None = None


class ChatService:
    def __init__(
        self,
        uow: UnitOfWork,
        store: ChatPersistence,
        corpus: AuthorizedCorpus,
        provider: Models,
        ledger: RunJournal,
        barrier: AuthorizationBarrier,
        settings: Settings,
        cpu: ProcessPoolExecutor,
    ) -> None:
        (
            self.uow,
            self.store,
            self.corpus,
            self.provider,
            self.ledger,
            self.barrier,
            self.settings,
            self.cpu,
        ) = uow, store, corpus, provider, ledger, barrier, settings, cpu
        self.cpu_slots = asyncio.Semaphore(2)

    async def prepare(
        self,
        principal: Principal,
        token: str,
        session_id: str,
        request_id: str,
        question: str,
        started: float | None = None,
    ) -> Prepared:
        started = started or time.monotonic()
        async with self.barrier.read():
            async with self.uow() as repos:
                current = await repos.identity.principal(digest_token(token))
                if current is None:
                    raise AppError("unauthorized", 401, "登录已失效")
                session = await repos.conversations.session(current, session_id)
                revision = await repos.access.revision()
            requested = tuple(session["kb_ids"] or ([session["kb_id"]] if session["kb_id"] else []))
            scope = await self.corpus.bind(current, requested, revision)
            message, history = await self.store.prepare(
                current, session_id, request_id, question, scope, "baseline"
            )
            try:
                run_id = await self.ledger.start_run(
                    message["id"],
                    {
                        "kind": "chat",
                        "strategy": "baseline",
                        "graph": "baseline-graph-v1",
                        "scope_hash": scope.scope_hash,
                        "snapshot_members": list(scope.snapshots),
                        "prompt_hashes": PROMPT_HASHES,
                        "chat_model": self.settings.chat_model,
                        "embedding_model": self.settings.embedding_model,
                        "rerank_model": self.settings.rerank_model,
                        "token_limit_mode": "estimate",
                    },
                )
            except BaseException:
                await self.store.abort(message["id"], "error", "ledger_unavailable")
                raise
        run = ChatRunInput(
            request_id,
            message["id"],
            current,
            revision,
            requested,
            question,
            tuple(history),
            "baseline",
            started + self.settings.request_timeout,
        )
        return Prepared(run, scope, token, run_id, started)

    async def guard(self, prepared: Prepared) -> None:
        if time.monotonic() >= prepared.run.deadline:
            raise AppError("deadline", 504, "请求达到时间上限")
        async with self.uow() as repos:
            current = await repos.identity.principal(digest_token(prepared.token))
        if current is None:
            raise AppError("authorization_changed", 409, "登录或权限已失效")
        await self.corpus.check(current, prepared.scope)

    def runner(self, prepared: Prepared) -> GraphRunner:
        async def guard() -> None:
            await self.guard(prepared)

        models = ModelSession(
            self.provider,
            Budget(prepared.run.deadline, self.settings.request_calls, self.settings.token_reservation_limit),
            guard,
            self.ledger,
            prepared.run_id,
        )
        return GraphRunner(
            Context(prepared.run, prepared.scope, models, self.corpus, self.cpu, self.cpu_slots)
        )

    def public_summary(self, prepared: Prepared, runner: GraphRunner, reason: str = "finished") -> Row:
        return {
            "elapsed_ms": round((time.monotonic() - prepared.started) * 1000),
            "first_progress_ms": prepared.first_progress_ms,
            "server_ttft_ms": prepared.server_ttft_ms,
            "stop_reason": reason,
            **{
                k: v
                for k, v in runner.context.models.summary().items()
                if k
                in {
                    "calls",
                    "input_tokens",
                    "output_tokens",
                    "unknown_usage_calls",
                    "reserved_token_estimate",
                    "token_limit_is_estimated",
                }
            },
        }
