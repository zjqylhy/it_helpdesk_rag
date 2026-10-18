import asyncio
import math
import time
from collections.abc import Awaitable, Callable

from helpdesk.application.model_session import Budget, ModelSession
from helpdesk.domain.errors import AppError
from helpdesk.domain.rag import Lease, ParsedChunk
from helpdesk.ports.jobs import JobQueue
from helpdesk.ports.models import Models, RunJournal
from helpdesk.ports.repositories import FileStore
from helpdesk.settings import Settings


class IngestionWorker:
    def __init__(
        self,
        jobs: JobQueue,
        files: FileStore,
        models: Models,
        ledger: RunJournal,
        settings: Settings,
        parser: Callable[[bytes, str], Awaitable[list[ParsedChunk]]],
    ) -> None:
        self.jobs, self.files, self.models, self.ledger, self.settings, self.parser = (
            jobs,
            files,
            models,
            ledger,
            settings,
            parser,
        )

    async def process(self, lease: Lease) -> None:
        run_id = await self.ledger.start_run(
            None,
            {
                "kind": "ingestion",
                "job_id": lease.job_id,
                "version_id": lease.version_id,
                "embedding_model": self.settings.embedding_model,
                "dimension": self.settings.embedding_dimension,
                "parser_version": "rewrite-paragraph-700-120-v1",
            },
        )

        async def guard() -> None:
            await self.jobs.heartbeat(lease)

        session = ModelSession(
            self.models,
            Budget(time.monotonic() + self.settings.worker_job_timeout, 512, 10_000_000),
            guard,
            self.ledger,
            run_id,
        )

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(self.settings.worker_heartbeat_seconds)
                await self.jobs.heartbeat(lease)

        async def work() -> None:
            data = await self.files.read(lease.storage_key, lease.content_hash)
            chunks = await self.parser(data, lease.filename)
            if not chunks or len(chunks) > 5000:
                raise AppError("invalid_parse", 400, "切片数量无效")
            vectors = []
            for start in range(0, len(chunks), 20):
                batch = chunks[start : start + 20]
                embedded = await session.embed([c.text for c in batch])
                if len(embedded) != len(batch) or any(
                    len(v) != self.settings.embedding_dimension
                    or not any(v)
                    or any(type(x) not in {float, int} or not math.isfinite(x) for x in v)
                    for v in embedded
                ):
                    raise AppError("invalid_embedding", 502, "向量维度或数量无效")
                vectors.extend(embedded)
                await self.jobs.heartbeat(lease, min(90, int(90 * len(vectors) / len(chunks))))
            await self.jobs.publish(
                lease, chunks, vectors, self.settings.embedding_model, self.settings.embedding_dimension
            )

        task = asyncio.create_task(work())
        beat = asyncio.create_task(heartbeat())
        try:
            async with asyncio.timeout(self.settings.worker_job_timeout):
                done, _ = await asyncio.wait({task, beat}, return_when=asyncio.FIRST_COMPLETED)
                for finished in done:
                    await finished
            await self.ledger.finish_run(run_id, "completed", session.summary())
        except BaseException as error:
            code = (
                error.code
                if isinstance(error, AppError)
                else "worker_interrupted"
                if isinstance(error, asyncio.CancelledError)
                else "ingestion_failed"
            )
            await self.jobs.fail(lease, code)
            await self.ledger.finish_run(run_id, "error", {"stop_reason": code, **session.summary()})
            if isinstance(error, asyncio.CancelledError):
                raise
        finally:
            task.cancel()
            beat.cancel()
            await asyncio.gather(task, beat, return_exceptions=True)

    async def once(self) -> bool:
        lease = await self.jobs.claim()
        if lease is None:
            return False
        await self.process(lease)
        return True
