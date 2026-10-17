import asyncio
import json
import time
from dataclasses import dataclass

from fastapi.responses import Response
from starlette.types import Receive, Scope, Send

from helpdesk.application.chat import ChatService, Prepared
from helpdesk.domain.contracts import PublicEvent
from helpdesk.domain.errors import AppError
from helpdesk.graph.baseline import Outcome

ALLOWED = {
    "status": {"stage", "message"},
    "evidence": {"behavior", "snapshot_ids", "evidence"},
    "answer_delta": {"text"},
    "citations": {"citations"},
    "refusal": {"behavior", "message"},
}


@dataclass(frozen=True)
class Failure:
    error: Exception


class ChatResponse(Response):
    media_type = "text/event-stream"

    def __init__(self, service: ChatService, prepared: Prepared) -> None:
        super().__init__(headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        self.service, self.prepared = service, prepared
        self.raw_headers = [(k, v) for k, v in self.raw_headers if k != b"content-length"]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        service, prepared = self.service, self.prepared
        runner = service.runner(prepared)
        queue: asyncio.Queue[PublicEvent | Outcome | Failure] = asyncio.Queue(maxsize=16)
        seq = 0
        sent_done = False
        key = prepared.run.message_id
        main = asyncio.current_task()
        assert main is not None

        async def frame(event: str, data: dict, *, guard: bool = True) -> None:
            nonlocal seq
            seq += 1
            payload = {**data, "request_id": prepared.run.request_id, "message_id": key, "seq": seq}
            encoded = (
                "event: " + event + "\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
            ).encode()
            if guard:
                async with service.barrier.read():
                    await service.guard(prepared)
                    async with asyncio.timeout(5):
                        await send({"type": "http.response.body", "body": encoded, "more_body": True})
                elapsed = round((time.monotonic() - prepared.started) * 1000, 2)
                if event == "status" and prepared.first_progress_ms is None:
                    prepared.first_progress_ms = elapsed
                if event == "answer_delta" and data.get("text") and prepared.server_ttft_ms is None:
                    prepared.server_ttft_ms = elapsed
            else:
                async with asyncio.timeout(2):
                    await send({"type": "http.response.body", "body": encoded, "more_body": True})

        async def produce() -> None:
            try:
                async for event in runner.events():
                    await queue.put(event)
                if runner.outcome is None:
                    raise AppError("graph_incomplete", 502, "问答流程未正常结束")
                await queue.put(runner.outcome)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await queue.put(Failure(error))

        async def disconnected() -> None:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    main.cancel("client_disconnected")
                    return

        producer = None
        monitor = None
        try:
            async with asyncio.timeout(5):
                await send({"type": "http.response.start", "status": 200, "headers": self.raw_headers})
            async with service.barrier.read():
                await service.guard(prepared)
                service.barrier.register(key, prepared.scope.revision, main)
            producer = asyncio.create_task(produce())
            monitor = asyncio.create_task(disconnected())
            async with asyncio.timeout(max(0.001, prepared.run.deadline - time.monotonic())):
                while True:
                    try:
                        item = await asyncio.wait_for(
                            queue.get(), timeout=min(10, max(0.001, prepared.run.deadline - time.monotonic()))
                        )
                    except TimeoutError:
                        async with service.barrier.read():
                            await service.guard(prepared)
                            async with asyncio.timeout(5):
                                await send(
                                    {
                                        "type": "http.response.body",
                                        "body": b": keepalive\n\n",
                                        "more_body": True,
                                    }
                                )
                        continue
                    if isinstance(item, Failure):
                        raise item.error
                    if isinstance(item, Outcome):
                        summary = service.public_summary(prepared, runner)
                        async with service.barrier.read():
                            await service.guard(prepared)
                            await service.store.finish(key, item.status, item.answer, item.citations, summary)
                            await service.ledger.finish_run(
                                prepared.run_id,
                                item.status,
                                {
                                    **item.trace,
                                    **summary,
                                    **runner.diagnostics(),
                                    "usage": runner.context.models.summary(),
                                    "attempt_details": runner.context.models.attempt_details,
                                },
                            )
                        await frame(
                            "done",
                            {
                                "status": item.status,
                                "completion_kind": "full" if item.status == "completed" else None,
                                "trace": summary,
                            },
                        )
                        sent_done = True
                        break
                    allowed = ALLOWED.get(item.event)
                    if allowed is None or not set(item.data).issubset(allowed):
                        raise AppError("unsafe_event", 500, "公开事件字段无效")
                    await frame(item.event, item.data)
        except BaseException as error:
            service.barrier.unregister(key)
            # 先关闭图与上游 HTTP，再做有界终态清理，避免断连后仍继续模型业务。
            if producer is not None:
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
            code = (
                error.code
                if isinstance(error, AppError)
                else "interrupted"
                if isinstance(error, asyncio.CancelledError)
                else "deadline"
                if isinstance(error, TimeoutError)
                else "generation_failed"
            )
            status = "interrupted" if isinstance(error, asyncio.CancelledError) else "error"
            if not sent_done:
                try:
                    async with asyncio.timeout(5):
                        await asyncio.shield(service.store.abort(key, status, code))
                        await asyncio.shield(
                            service.ledger.finish_run(
                                prepared.run_id,
                                status,
                                {
                                    "stop_reason": code,
                                    **runner.diagnostics(),
                                    "usage": runner.context.models.summary(),
                                    "attempt_details": runner.context.models.attempt_details,
                                },
                            )
                        )
                    await frame(
                        "error", {"code": code, "message": "回答未完成，请刷新会话后重试"}, guard=False
                    )
                except (Exception, asyncio.CancelledError):
                    pass
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            service.barrier.unregister(key)
            for task in [producer, monitor]:
                if task:
                    task.cancel()
            await asyncio.gather(*(t for t in [producer, monitor] if t), return_exceptions=True)
            try:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            except (Exception, asyncio.CancelledError):
                pass
