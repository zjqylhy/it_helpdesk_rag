import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from helpdesk.domain.errors import AppError
from helpdesk.domain.models import Row
from helpdesk.domain.rag import Usage
from helpdesk.ports.models import AttemptLedger, Models


class TransientModelError(AppError):
    def __init__(self) -> None:
        super().__init__("provider_transient", 502, "模型服务暂时不可用")


@dataclass
class Budget:
    deadline: float
    max_calls: int = 24
    reservation_limit: int = 48000
    calls: int = 0
    reserved: int = 0

    def remaining(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise AppError("deadline", 504, "请求已达到时间上限")
        return left

    def reserve(self, payload: object, output: int) -> int:
        self.remaining()
        # UTF-8 字节数用于保守工程估算，不能冒充与提供商 tokenizer 匹配的硬 Token 保证。
        estimate = len(json.dumps(payload, ensure_ascii=False).encode()) + output
        if self.calls >= self.max_calls or self.reserved + estimate > self.reservation_limit:
            raise AppError("budget_exhausted", 429, "本次请求预算已用尽")
        self.calls += 1
        self.reserved += estimate
        return estimate


class ModelSession:
    def __init__(
        self,
        provider: Models,
        budget: Budget,
        guard: Callable[[], Awaitable[None]],
        ledger: AttemptLedger,
        run_id: str,
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

    async def call[T](
        self, stage: str, payload: object, limit: int, operation: Callable[[], Awaitable[tuple[T, Usage]]]
    ) -> T:
        for attempt in range(2):
            await self.guard()
            reserved = self.budget.reserve(payload, limit)
            attempt_id = await self.ledger.start_attempt(self.run_id, stage, reserved)
            self.started_calls += 1
            try:
                async with asyncio.timeout(min(25, self.budget.remaining())):
                    value, usage = await operation()
                await self.ledger.end_attempt(attempt_id, "completed", usage)
                self.usages.append(usage)
                return value
            except BaseException as error:
                self.usages.append(Usage())
                async with asyncio.timeout(3):
                    await asyncio.shield(
                        self.ledger.end_attempt(
                            attempt_id,
                            "unknown" if isinstance(error, asyncio.CancelledError) else "error",
                            Usage(),
                        )
                    )
                if not isinstance(error, TransientModelError) or attempt:
                    raise
                await asyncio.sleep(min(0.2, self.budget.remaining()))
        raise AssertionError("unreachable")

    async def structured(self, stage: str, system: str, payload: Row, limit: int = 512) -> Row:
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
        await self.guard()
        reserved = self.budget.reserve({"system": system, "payload": payload}, 2048)
        attempt_id = await self.ledger.start_attempt(self.run_id, "generate", reserved)
        self.started_calls += 1
        usage = Usage()
        finish = None
        total = 0
        try:
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
            await self.ledger.end_attempt(attempt_id, "completed", usage)
            self.usages.append(usage)
        except BaseException:
            self.usages.append(usage)
            async with asyncio.timeout(3):
                await asyncio.shield(self.ledger.end_attempt(attempt_id, "unknown", usage))
            raise

    def summary(self) -> Row:
        known = all(u.input_tokens is not None and u.output_tokens is not None for u in self.usages)
        return {
            "calls": self.started_calls,
            "input_tokens": sum(u.input_tokens or 0 for u in self.usages) if known else None,
            "output_tokens": sum(u.output_tokens or 0 for u in self.usages) if known else None,
            "unknown_usage_calls": sum(
                u.input_tokens is None or u.output_tokens is None for u in self.usages
            ),
            "reserved_token_estimate": self.budget.reserved,
            "token_limit_is_estimated": True,
        }
