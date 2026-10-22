import asyncio
import logging
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

LOGGER = logging.getLogger(__name__)
MAX_ANSWER_BYTES = 256 * 1024


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
        self._admission_lock = asyncio.Lock()
        self._admissions: set[asyncio.Task[tuple[Row, bool]]] = set()
        self._runs: dict[str, asyncio.Task[None]] = {}
        self._closing = False

    async def submit(
        self, principal: Principal, token: str, session_id: str, request_id: str, question: str
    ) -> tuple[Row, bool]:
        if self._closing:
            raise AppError("shutting_down", 503, "服务正在关闭，请稍后重试")
        # 受理和执行均由应用持有；浏览器放弃等待不撤销已经提交的任务。
        task = asyncio.create_task(self._accept(principal, token, session_id, request_id, question))
        self._admissions.add(task)

        def consumed(done: asyncio.Task[tuple[Row, bool]]) -> None:
            self._admissions.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(consumed)
        return await asyncio.shield(task)

    async def _accept(
        self, principal: Principal, token: str, session_id: str, request_id: str, question: str
    ) -> tuple[Row, bool]:
        async with self._admission_lock:
            if self._closing:
                raise AppError("shutting_down", 503, "服务正在关闭，请稍后重试")
            async with self.uow() as repos:
                await repos.conversations.session(principal, session_id)
                rows = await repos.conversations.messages(session_id)
            existing = next((row for row in rows if row["request_id"] == request_id), None)
            if existing is not None:
                if existing["question"] != question:
                    raise AppError("conflict", 409, "request_id 已用于其他问题")
                return self.receipt(existing), False
            prepared = await self.prepare(principal, token, session_id, request_id, question)
            try:
                runner = self.runner(prepared)
                async with self.barrier.read():
                    await self.guard(prepared)
                    if self._closing:
                        raise AppError("shutting_down", 503, "服务正在关闭，请稍后重试")
                    task = asyncio.create_task(self._execute(prepared, runner))
                    self._runs[prepared.run.message_id] = task
                    self.barrier.register(prepared.run.message_id, prepared.scope.revision, task)
            except BaseException:
                await self.store.abort(prepared.run.message_id, "error", "admission_failed")
                await self.ledger.finish_run(prepared.run_id, "error", {"stop_reason": "admission_failed"})
                raise
            return {
                "message_id": prepared.run.message_id,
                "request_id": request_id,
                "status": "generating",
            }, True

    @staticmethod
    def receipt(message: Row) -> Row:
        return {"message_id": message["id"], "request_id": message["request_id"], "status": message["status"]}

    async def stop(self, principal: Principal, session_id: str, message_id: str) -> Row:
        async def current() -> Row:
            async with self.uow() as repos:
                await repos.conversations.session(principal, session_id)
                rows = await repos.conversations.messages(session_id)
            message = next((row for row in rows if row["id"] == message_id), None)
            if message is None:
                raise AppError("not_found", 404, "资源不存在或不可访问")
            return message

        # 消息已写入但受理仍在初始化时，等待交接完成后再停止，避免漏取消。
        async with self._admission_lock:
            message = await current()
            task = self._runs.get(message_id)
            if message["status"] == "generating" and task is not None and not task.cancelling():
                task.cancel("user_stopped")
        if message["status"] == "generating" and task is not None:
            # 等待执行侧关闭上游并记录终态，停止请求断连也不再次取消收尾。
            try:
                async with asyncio.timeout(6):
                    await asyncio.shield(task)
            except (TimeoutError, asyncio.CancelledError):
                pass
            message = await current()
        return self.receipt(message)

    async def close(self) -> None:
        self._closing = True
        tasks = [*self._admissions, *self._runs.values()]
        for task in tasks:
            if not task.cancelling():
                task.cancel("api_shutdown")
        if tasks:
            try:
                async with asyncio.timeout(6):
                    await asyncio.gather(*tasks, return_exceptions=True)
            except TimeoutError:
                LOGGER.error("问答关闭清理超时，未完成状态由启动恢复收束")

    async def _execute(self, prepared: Prepared, runner: GraphRunner) -> None:
        key = prepared.run.message_id
        answer = ""
        saved = ""
        children: list[asyncio.Task[None]] = []

        async def collect() -> None:
            nonlocal answer
            async for event in runner.events():
                elapsed = round((time.monotonic() - prepared.started) * 1000, 2)
                if event.event == "status" and prepared.first_progress_ms is None:
                    prepared.first_progress_ms = elapsed
                if event.event == "answer_delta":
                    text = event.data["text"]
                    if not isinstance(text, str):
                        raise AppError("invalid_generation", 502, "回答文字格式无效")
                    answer += text
                    if text and prepared.server_ttft_ms is None:
                        prepared.server_ttft_ms = elapsed
                if len(answer.encode("utf-8")) > MAX_ANSWER_BYTES:
                    raise AppError("answer_too_large", 502, "回答内容超出长度限制")

        async def flush() -> None:
            nonlocal saved
            while True:
                await asyncio.sleep(1)
                async with self.barrier.read():
                    await self.guard(prepared)
                    if answer != saved:
                        value = answer
                        await self.store.progress(key, value)
                        saved = value

        async def stop_children() -> None:
            for child in children:
                child.cancel()
            await asyncio.gather(*children, return_exceptions=True)

        try:
            producer = asyncio.create_task(collect())
            writer = asyncio.create_task(flush())
            children = [producer, writer]
            async with asyncio.timeout(max(0.001, prepared.run.deadline - time.monotonic())):
                done, _ = await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
                for child in done:
                    await child
                if producer not in done:
                    raise AppError("progress_failed", 500, "进度保存未正常结束")
                await stop_children()
                outcome = runner.outcome
                if outcome is None:
                    raise AppError("graph_incomplete", 502, "问答流程未正常结束")
                summary = self.public_summary(prepared, runner)
                async with self.barrier.read():
                    await self.guard(prepared)
                    await self.store.finish(key, outcome.status, outcome.answer, outcome.citations, summary)
                await self.ledger.finish_run(
                    prepared.run_id,
                    outcome.status,
                    {
                        **outcome.trace,
                        **summary,
                        **runner.diagnostics(),
                        "usage": runner.context.models.summary(),
                        "attempt_details": runner.context.models.attempt_details,
                    },
                )
        except BaseException as error:
            await stop_children()
            code = (
                str(error.args[0])
                if isinstance(error, asyncio.CancelledError) and error.args
                else "interrupted"
                if isinstance(error, asyncio.CancelledError)
                else error.code
                if isinstance(error, AppError)
                else "deadline"
                if isinstance(error, TimeoutError)
                else "generation_failed"
            )
            status = "interrupted" if isinstance(error, asyncio.CancelledError) else "error"
            try:
                async with asyncio.timeout(5):
                    actual_status = await self.store.abort(key, status, code)
                    await self.ledger.finish_run(
                        prepared.run_id,
                        actual_status,
                        {
                            "stop_reason": code if actual_status == status else "finished",
                            **runner.diagnostics(),
                            "usage": runner.context.models.summary(),
                            "attempt_details": runner.context.models.attempt_details,
                        },
                    )
            except (Exception, asyncio.CancelledError):
                LOGGER.exception("问答终态清理失败 message_id=%s", key)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            self.barrier.unregister(key)
            self._runs.pop(key, None)

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
