import asyncio
import json
import time

import httpx
import pytest
from fakes import FakeModels

from helpdesk.adapters.models.gateway import HttpModels
from helpdesk.application.evaluation import ExperimentLedger
from helpdesk.application.model_session import Budget, ModelSession, TransientModelError
from helpdesk.domain.errors import AppError
from helpdesk.domain.rag import Usage
from helpdesk.settings import Settings


@pytest.mark.parametrize("streaming", [False, True])
async def test_permission_loss_during_attempt_reservation_prevents_provider_send(
    tmp_path, monkeypatch, streaming,
):
    provider = FakeModels()
    ledger = ExperimentLedger(tmp_path / "dispatch.sqlite", 4, 100000)
    revoked = False
    original = ledger.start_attempt

    async def guard():
        if revoked:
            raise AppError("authorization_changed", 409, "权限已变化")

    async def reserve(*args):
        nonlocal revoked
        attempt = await original(*args)
        revoked = True
        return attempt

    monkeypatch.setattr(ledger, "start_attempt", reserve)
    session = ModelSession(provider, Budget(time.monotonic() + 10), guard, ledger, "dispatch")
    try:
        with pytest.raises(AppError) as error:
            if streaming:
                _ = [text async for text in session.generate("system", {})]
            else:
                await session.embed(["protected content"])
        assert error.value.code == "authorization_changed"
        assert provider.calls == []
        assert ledger.conn.execute("SELECT status FROM attempts").fetchone() == ("error",)
    finally:
        ledger.close()


def settings():
    return Settings(
        database_url="postgresql://unused",
        upload_dir="unused",
        chat_url="http://localhost/chat",
        chat_model="test",
        embedding_url="http://localhost/embed",
        embedding_model="test",
        embedding_dimension=3,
        rerank_url="http://localhost/rank",
        rerank_model="test",
        rerank_format="generic",
    )


async def test_http_protocol_validates_vectors_ranking_and_wire_usage():
    seen = []

    def handle(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if request.url.path == "/embed":
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 2, 3]}]})
        if request.url.path == "/rank":
            return httpx.Response(
                200,
                json={
                    "results": [{"index": 0, "relevance_score": 0.1}, {"index": 1, "relevance_score": 0.9}],
                    "usage": {"input_tokens": 8, "output_tokens": 0},
                },
            )
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": '{"behavior":"answer"}'}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = HttpModels(settings(), client)
        assert (await provider.embed(["资料"])) == ([[1, 2, 3]], Usage(None, 0))
        assert (await provider.rank("问题", ["一", "二"])) == ([1, 0], Usage(8, 0))
        assert (await provider.structured("sufficiency", "system", {"question": "问题"}, 512)).value == {
            "behavior": "answer"
        }
        assert seen[0]["dimensions"] == 3 and seen[1]["top_n"] == 2


@pytest.mark.parametrize(
    "body,operation",
    [
        ({"data": [{"index": 0, "embedding": [0, 0, 0]}]}, "embed"),
        ({"data": [{"index": 0, "embedding": [1, 2]}]}, "embed"),
        ({"data": [{"index": 1, "embedding": [1, 2, 3]}]}, "embed"),
        ({"results": [{"index": 0}, {"index": 0}]}, "rank"),
        ({"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}, "structured"),
    ],
)
async def test_malformed_provider_rejected(body, operation):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    ) as client:
        provider = HttpModels(settings(), client)
        with pytest.raises(AppError, match="格式"):
            if operation == "embed":
                await provider.embed(["test"])
            elif operation == "rank":
                await provider.rank("test", ["one", "two"])
            else:
                await provider.structured("scope", "system", {}, 512)


async def test_sse_usage_after_finish_and_missing_done():
    raw = "\r\n".join(
        [
            ": heartbeat",
            'data: {"choices":[{"delta":{"content":"中文"},"finish_reason":null}]}',
            "",
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "",
            'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}',
            "",
            "data: [DONE]",
            "",
            "",
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=raw))
    ) as client:
        chunks = [c async for c in HttpModels(settings(), client).generate("system", {}, 2048)]
        assert "".join(c.text for c in chunks) == "中文"
        assert chunks[-1].usage == Usage(5, 2)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text=raw.replace("data: [DONE]", ": missing"))
        )
    ) as client:
        with pytest.raises(AppError, match="结束标记"):
            [c async for c in HttpModels(settings(), client).generate("system", {}, 2048)]


async def test_retries_share_durable_experiment_and_request_budgets(tmp_path):
    ledger = ExperimentLedger(tmp_path / "ledger.sqlite", 2, 10000)
    provider = FakeModels()
    calls = 0

    async def unstable(texts):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TransientModelError()
        return [[1.0, 2.0, 3.0]], Usage(3, 0)

    provider.embed = unstable

    async def guard():
        pass

    session = ModelSession(provider, Budget(time.monotonic() + 5, 3, 10000), guard, ledger, "run")
    assert await session.embed(["text"]) == [[1.0, 2.0, 3.0]]
    with pytest.raises(AppError, match="全实验预算"):
        await session.embed(["text"])
    assert calls == 2 and ledger.summary()["calls"] == 2
    assert session.summary()["input_tokens"] is None
    ledger.close()
    reopened = ExperimentLedger(tmp_path / "ledger.sqlite", 2, 10000)
    with pytest.raises(AppError):
        await reopened.start_attempt("other", "embedding", 1)
    reopened.close()


def test_reservation_deadline_and_slot_limits_before_http():
    for budget in [
        Budget(time.monotonic() - 1),
        Budget(time.monotonic() + 1, 0),
        Budget(time.monotonic() + 1, 1, 1),
    ]:
        with pytest.raises(AppError):
            budget.reserve("input", 10)


async def test_stream_cancellation_closes_upstream_and_marks_unknown(tmp_path):
    closed = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Stream()))
    ) as client:
        ledger = ExperimentLedger(tmp_path / "ledger.sqlite", 10, 10000)

        async def guard():
            pass

        session = ModelSession(
            HttpModels(settings(), client), Budget(time.monotonic() + 10), guard, ledger, "cancel"
        )
        started = asyncio.Event()

        async def consume():
            async for _ in session.generate("system", {}):
                started.set()

        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        assert ledger.conn.execute("SELECT status FROM attempts").fetchone() == ("unknown",)
        ledger.close()
