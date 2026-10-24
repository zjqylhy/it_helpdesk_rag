import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass

from helpdesk.application.scheduling import rate_units, route_kind
from helpdesk.domain.errors import AppError
from helpdesk.domain.models import Row
from helpdesk.domain.rag import Usage
from helpdesk.ports.models import AttemptLedger, ModelAdmission, Models


class TransientModelError(AppError):
    def __init__(self, status_code: int | None = None, request_id: str | None = None) -> None:
        super().__init__("provider_transient", 502, "模型服务暂时不可用")
        self.status_code, self.request_id = status_code, request_id


class RateLimitedModelError(TransientModelError):
    def __init__(self, seconds: float, source: str, request_id: str | None = None) -> None:
        super().__init__(429, request_id)
        self.code, self.message = "provider_rate_limited", "模型服务达到速率上限"
        self.status = 429
        self.retry_after, self.retry_source = seconds, source


@dataclass
class Budget:
    deadline: float
    max_calls: int = 24
    reservation_limit: int = 48000
    calls: int = 0
    reserved: int = 0
    clock: Callable[[], float] = time.monotonic

    def remaining(self) -> float:
        left = self.deadline - self.clock()
        if left <= 0:
            raise AppError("deadline", 504, "请求已达到时间上限")
        return left

    def reserve(self, payload: object, output: int) -> int:
        estimate = self.check(payload, output)
        self.calls += 1
        self.reserved += estimate
        return estimate

    def check(self, payload: object, output: int) -> int:
        self.remaining()
        # UTF-8 字节数用于保守工程估算，不能冒充与提供商 tokenizer 匹配的硬 Token 保证。
        estimate = self.estimate(payload, output)
        if self.calls >= self.max_calls or self.reserved + estimate > self.reservation_limit:
            raise AppError("budget_exhausted", 429, "本次请求预算已用尽")
        return estimate

    @staticmethod
    def estimate(payload: object, output: int) -> int:
        return len(json.dumps(payload, ensure_ascii=False).encode()) + output


class ModelSession:
    def __init__(
        self,
        provider: Models,
        budget: Budget,
        guard: Callable[[], Awaitable[None]],
        ledger: AttemptLedger,
        run_id: str,
        admission: ModelAdmission | None = None,
    ) -> None:
        self.provider, self.budget, self.guard, self.ledger, self.run_id = (
            provider,
            budget,
            guard,
            ledger,
            run_id,
        )
        self.usages: list[Usage] = []
        self.started_calls = 0
        self.admission = admission
        self.wait_seconds = 0.0
        self.attempt_details: list[Row] = []

    async def start(self, stage: str, payload: object, limit: int) -> str:
        await self.guard()
        estimate = self.budget.check(payload, limit)
        await self.ledger.check_attempt(estimate)
        if self.admission is not None:
            started = self.budget.clock()
            try:
                await self.admission.acquire(
                    route_kind(stage), rate_units(stage, payload, limit), self.budget.deadline, self.guard
                )
            finally:
                self.wait_seconds += self.budget.clock() - started
        await self.guard()
        reserved = self.budget.reserve(payload, limit)
        attempt_id = await self.ledger.start_attempt(self.run_id, stage, reserved)
        self.started_calls += 1
        self.attempt_details.append({"attempt_id": attempt_id, "stage": stage, "status": "started"})
        return attempt_id

    async def finish(
        self, attempt_id: str, status: str, usage: Usage, error: BaseException | None = None
    ) -> None:
        details = next(d for d in self.attempt_details if d["attempt_id"] == attempt_id)
        details.update(status=status, usage=asdict(usage))
        if error is not None:
            details["error_code"] = error.code if isinstance(error, AppError) else type(error).__name__
        if isinstance(error, TransientModelError):
            details.update(http_status=error.status_code, provider_request_id=error.request_id)
        if isinstance(error, RateLimitedModelError):
            details.update(retry_after_seconds=error.retry_after, retry_after_source=error.retry_source)
            if self.admission is not None:
                await self.admission.cool_down(route_kind(details["stage"]), error.retry_after)
        await self.ledger.end_attempt(attempt_id, status, usage)

    async def wait_retry(self, seconds: float) -> None:
        if seconds >= self.budget.remaining():
            raise AppError("model_rate_deadline", 504, "模型等待超过请求剩余时限")
        started = self.budget.clock()
        try:
            due = self.budget.clock() + seconds
            while self.budget.clock() < due:
                await self.guard()
                await asyncio.sleep(min(1, due - self.budget.clock(), self.budget.remaining()))
        finally:
            self.wait_seconds += self.budget.clock() - started

    async def call[T](
        self, stage: str, payload: object, limit: int, operation: Callable[[], Awaitable[tuple[T, Usage]]]
    ) -> T:
        for attempt in range(2):
            attempt_id = await self.start(stage, payload, limit)
            observed_usage = Usage(
                output_applicable=route_kind(stage) == "chat",
                output_source="not_applicable" if route_kind(stage) != "chat" else None,
            )
            try:
                # 账本写入可能等待数据库；发送前必须再次检查期间发生的撤权。
                await self.guard()
                async with asyncio.timeout(min(25, self.budget.remaining())):
                    value, usage = await operation()
                observed_usage = usage
                await self.finish(attempt_id, "completed", usage)
                self.usages.append(usage)
                return value
            except BaseException as error:
                reported = getattr(error, "known_usage", None)
                if isinstance(reported, Usage):
                    observed_usage = reported
                self.usages.append(observed_usage)
                async with asyncio.timeout(3):
                    await asyncio.shield(
                        self.finish(
                            attempt_id,
                            "unknown" if isinstance(error, asyncio.CancelledError) else "error",
                            observed_usage,
                            error,
                        )
                    )
                if not isinstance(error, TransientModelError) or attempt:
                    raise
                if isinstance(error, RateLimitedModelError):
                    # 共享调度器持久化冷却；没有调度器的线上调用仍遵守服务端等待。
                    if self.admission is None:
                        await self.wait_retry(error.retry_after)
                else:
                    await self.wait_retry(0.5)
        raise AssertionError("unreachable")

    async def structured(self, stage: str, system: str, payload: Row, limit: int = 2048) -> Row:
        async def operation() -> tuple[Row, Usage]:
            result = await self.provider.structured(stage, system, payload, limit)
            return result.value, result.usage

        return await self.call(stage, {"system": system, "payload": payload}, limit, operation)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self.call("embedding", texts, 0, lambda: self.provider.embed(texts))

    async def rank(self, query: str, texts: list[str]) -> list[int]:
        return await self.call(
            "rerank", {"query": query, "documents": texts}, 0, lambda: self.provider.rank(query, texts)
        )

    async def generate(self, system: str, payload: Row) -> AsyncIterator[str]:
        attempt_id = await self.start("generate", {"system": system, "payload": payload}, 2048)
        usage = Usage()
        finish = None
        total = 0
        try:
            await self.guard()
            async with asyncio.timeout(min(25, self.budget.remaining())):
                async for chunk in self.provider.generate(system, payload, 2048):
                    await self.guard()
                    if chunk.usage.input_tokens is not None or chunk.usage.output_tokens is not None:
                        usage = chunk.usage
                    finish = chunk.finish_reason or finish
                    if chunk.text:
                        total += len(chunk.text.encode())
                        if total > 65536:
                            raise AppError("invalid_output", 502, "模型输出超出边界")
                        yield chunk.text
            if finish != "stop":
                raise AppError("incomplete_generation", 502, "模型回答未完整结束")
            await self.finish(attempt_id, "completed", usage)
            self.usages.append(usage)
        except BaseException as error:
            self.usages.append(usage)
            async with asyncio.timeout(3):
                await asyncio.shield(
                    self.finish(
                        attempt_id,
                        "unknown" if isinstance(error, asyncio.CancelledError) else "error",
                        usage,
                        error,
                    )
                )
            raise

    def summary(self) -> Row:
        return {
            "calls": self.started_calls,
            "input_tokens": sum(u.input_tokens or 0 for u in self.usages)
            if all(u.input_tokens is not None for u in self.usages)
            else None,
            "output_tokens": sum(u.output_tokens or 0 for u in self.usages)
            if all(u.output_tokens is not None for u in self.usages)
            else None,
            "known_input_tokens": sum(u.input_tokens or 0 for u in self.usages),
            "known_output_tokens": sum(u.output_tokens or 0 for u in self.usages),
            "output_not_applicable_calls": sum(not u.output_applicable for u in self.usages),
            "model_wait_ms": round(self.wait_seconds * 1000),
            "unknown_usage_calls": sum(
                u.input_tokens is None or u.output_tokens is None for u in self.usages
            ),
            "reserved_token_estimate": self.budget.reserved,
            "token_limit_is_estimated": True,
        }
