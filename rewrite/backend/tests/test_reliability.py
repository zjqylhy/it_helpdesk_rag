import asyncio
import json
from datetime import datetime, timezone
from email.utils import format_datetime

import httpx
import pytest
from fakes import FakeModels, rate_profile
from test_models_and_limits import settings

from helpdesk.adapters.models import gateway
from helpdesk.adapters.models.gateway import HttpModels, response_error, usage
from helpdesk.application.evaluation import ExperimentLedger
from helpdesk.application.model_session import Budget, ModelSession, RateLimitedModelError
from helpdesk.application.scheduling import RateGovernor, RateProfile, rate_units
from helpdesk.domain.errors import AppError
from helpdesk.domain.rag import Usage


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.wall_offset = 1700000000

    def monotonic(self):
        return self.value

    def utc(self):
        return self.wall_offset + self.value

    async def sleep(self, seconds):
        self.value += seconds
        await asyncio.sleep(0)


async def guard():
    return None


def profile(rpm=2, units=10000):
    return RateProfile.model_validate(rate_profile(settings(), rpm, units))


async def test_shared_window_minimum_spacing_and_independent_routes(tmp_path):
    clock = FakeClock()
    governor = RateGovernor(tmp_path / "schedule.db", profile(), clock)
    assert await governor.acquire("chat", 4000, 200, guard) == 0
    assert await governor.acquire("embedding", 4000, 200, guard) == 0
    assert await governor.acquire("chat", 4000, 200, guard) == 30
    assert await governor.acquire("chat", 4000, 200, guard) == 30
    assert clock.value == 60
    governor.close()


async def test_token_window_and_persistent_cooldown_resume(tmp_path):
    clock = FakeClock()
    path = tmp_path / "schedule.db"
    governor = RateGovernor(path, profile(rpm=1000), clock)
    await governor.acquire("chat", 9000, 200, guard)
    assert await governor.acquire("chat", 2000, 200, guard) == pytest.approx(60)
    await governor.cool_down("chat", 70)
    governor.close()
    resumed = RateGovernor(path, profile(rpm=1000), clock)
    with pytest.raises(AppError) as error:
        await resumed.acquire("chat", 100, clock.value + 20, guard)
    assert error.value.code == "model_rate_deadline"
    assert clock.value == pytest.approx(60)
    assert await resumed.acquire("chat", 100, 200, guard) == pytest.approx(70)
    resumed.close()


async def test_clock_rollback_profile_drift_and_oversized_requests_stop(tmp_path):
    clock = FakeClock()
    path = tmp_path / "schedule.db"
    governor = RateGovernor(path, profile(), clock)
    with pytest.raises(AppError) as error:
        await governor.acquire("chat", 10001, 200, guard)
    assert error.value.code == "rate_request_too_large"
    await governor.acquire("chat", 100, 200, guard)
    clock.wall_offset -= 10
    with pytest.raises(AppError) as error:
        await governor.acquire("chat", 100, 200, guard)
    assert error.value.code == "rate_clock_changed"
    governor.close()
    clock.wall_offset += 10
    with pytest.raises(ValueError, match="身份"):
        RateGovernor(path, profile(rpm=4), clock)


async def test_authorization_revoked_while_waiting_never_admits(tmp_path):
    clock = FakeClock()
    governor = RateGovernor(tmp_path / "schedule.db", profile(), clock)
    await governor.acquire("chat", 100, 200, guard)

    async def revoked():
        if clock.value >= 1:
            raise AppError("authorization_changed", 409, "changed")

    with pytest.raises(AppError) as error:
        await governor.acquire("chat", 100, 200, revoked)
    assert error.value.code == "authorization_changed"
    assert governor.conn.execute("SELECT count(*) FROM admissions").fetchone()[0] == 1
    governor.close()


@pytest.mark.parametrize(
    "header,expected,source",
    [
        (None, 60, "fallback"),
        ("garbage", 60, "fallback"),
        ("-1", 60, "fallback"),
        ("1.5", 60, "fallback"),
        ("0", 0, "seconds"),
        ("120", 120, "seconds"),
        ("2147483647", 2147483647, "seconds"),
    ],
)
def test_retry_after_parser_and_safe_error_metadata(header, expected, source):
    headers = {"x-request-id": "safe-request-1", "Authorization": "secret-never-copy"}
    if header is not None:
        headers["Retry-After"] = header
    error = response_error(httpx.Response(429, headers=headers, text="secret-body"))
    assert isinstance(error, RateLimitedModelError)
    assert (error.retry_after, error.retry_source, error.request_id) == (expected, source, "safe-request-1")
    assert error.status == 429 and "secret" not in repr(vars(error))


def test_http_date_and_past_date(monkeypatch):
    monkeypatch.setattr(gateway.time, "time", lambda: 1700000000)
    for offset, expected in [(30, 30), (-30, 0)]:
        date = format_datetime(datetime.fromtimestamp(1700000000 + offset, timezone.utc), usegmt=True)
        error = response_error(httpx.Response(429, headers={"retry-after": date}))
        assert error.retry_after == expected and error.retry_source == "http_date"


async def test_rate_retry_uses_same_ledger_waits_and_keeps_cooldown(tmp_path):
    clock = FakeClock()
    governor = RateGovernor(tmp_path / "schedule.db", profile(rpm=10000, units=100000), clock)
    ledger = ExperimentLedger(tmp_path / "ledger.db", 2, 100000)
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "65", "x-request-id": "req-429"})
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [1, 2, 3]}], "usage": {"total_tokens": 7}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        session = ModelSession(
            HttpModels(settings(), client), Budget(110, clock=clock.monotonic), guard, ledger, "run", governor
        )
        assert await session.embed(["query"]) == [[1, 2, 3]]
    assert clock.value == 65 and calls == ledger.summary()["calls"] == 2
    assert session.attempt_details[0]["retry_after_seconds"] == 65
    assert session.attempt_details[0]["http_status"] == 429
    assert session.summary()["model_wait_ms"] == 65000
    assert session.summary()["known_input_tokens"] == 7
    assert session.summary()["input_tokens"] is None
    assert session.usages[-1].output_applicable is False
    ledger.close()
    governor.close()


async def test_deadline_during_cooldown_does_not_consume_second_http_slot(tmp_path):
    clock = FakeClock()
    governor = RateGovernor(tmp_path / "schedule.db", profile(), clock)
    ledger = ExperimentLedger(tmp_path / "ledger.db", 5, 100000)
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        session = ModelSession(
            HttpModels(settings(), client), Budget(5, clock=clock.monotonic), guard, ledger, "one", governor
        )
        with pytest.raises(AppError) as error:
            await session.embed(["query"])
        assert error.value.code == "model_rate_deadline"
    assert calls == session.started_calls == ledger.summary()["calls"] == 1
    assert clock.value == 0
    ledger.close()
    governor.close()


async def test_exhausted_experiment_never_waits_or_opens_another_http_slot(tmp_path):
    clock = FakeClock()
    governor = RateGovernor(tmp_path / "schedule.db", profile(), clock)
    ledger = ExperimentLedger(tmp_path / "ledger.db", 1, 100000)
    await ledger.start_attempt("old", "scope", 10)
    session = ModelSession(FakeModels(), Budget(10, clock=clock.monotonic), guard, ledger, "new", governor)
    with pytest.raises(AppError) as error:
        await session.embed(["query"])
    assert error.value.code == "experiment_budget_exhausted"
    assert governor.conn.execute("SELECT count(*) FROM admissions").fetchone()[0] == 0
    ledger.close()
    governor.close()


def test_usage_input_only_total_is_not_chat_token_split():
    embedded = usage({"total_tokens": 17}, input_only=True)
    assert embedded.input_tokens == 17 and embedded.output_tokens == 0
    assert not embedded.output_applicable and embedded.raw_output_tokens is None
    assert embedded.input_source == "total_tokens_input_only" and embedded.output_source == "not_applicable"
    assert usage({"total_tokens": 17}) == Usage()
    assert usage({"prompt_tokens": 7}).input_tokens == 7
    assert usage({"prompt_tokens": True}, input_only=True).input_tokens is None


def test_rate_estimator_accounts_for_query_document_pairs():
    payload = {"query": "问题", "documents": ["one", "two", "three"]}
    single = len(json.dumps(payload, ensure_ascii=False).encode()) + 1024
    assert rate_units("rerank", payload, 0) == single + 2 * len("问题".encode())


def test_rate_profile_rejects_ambiguous_routes_and_alias_quota_splitting():
    value = rate_profile(settings())
    value["routes"]["chat"]["candidate_count"] = 2
    with pytest.raises(ValueError):
        RateProfile.model_validate(value)
    value = rate_profile(settings())
    value["routes"]["embedding"]["provider_id"] = "chat"
    with pytest.raises(ValueError, match="配额键"):
        RateProfile.model_validate(value)


async def test_corrupt_schedule_reservations_fail_closed(tmp_path):
    governor = RateGovernor(tmp_path / "schedule.db", profile(), FakeClock())
    await governor.acquire("chat", 100, 110, guard)
    with governor.conn:
        governor.conn.execute("UPDATE admissions SET units=-1")
    with pytest.raises(AppError) as error:
        await governor.acquire("chat", 100, 110, guard)
    assert error.value.code == "rate_state_invalid"
    governor.close()


def test_executed_schedule_cannot_be_reinitialized(tmp_path):
    with pytest.raises(ValueError, match="不可重新初始化"):
        RateGovernor(tmp_path / "empty.db", profile(), require_existing=True)


async def test_stream_rate_limit_is_never_replayed_and_sets_shared_cooldown(tmp_path):
    clock = FakeClock()
    governor = RateGovernor(tmp_path / "schedule.db", profile(units=100000), clock)
    ledger = ExperimentLedger(tmp_path / "ledger.db", 5, 100000)
    calls = 0

    def response(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"retry-after": "70"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        session = ModelSession(
            HttpModels(settings(), client),
            Budget(110, clock=clock.monotonic),
            guard,
            ledger,
            "stream",
            governor,
        )
        with pytest.raises(RateLimitedModelError):
            async for _ in session.generate("system", {}):
                pass
    assert calls == ledger.summary()["calls"] == 1
    assert session.attempt_details[0]["http_status"] == 429
    assert (
        governor.conn.execute("SELECT until FROM cooldowns WHERE quota='chat'").fetchone()[0]
        == clock.utc() + 70
    )
    ledger.close()
    governor.close()


async def test_cancellation_during_admission_has_no_http_attempt(tmp_path):
    class BlockingClock(FakeClock):
        async def sleep(self, seconds):
            waiting.set()
            await asyncio.Event().wait()

    waiting = asyncio.Event()
    clock = BlockingClock()
    governor = RateGovernor(tmp_path / "schedule.db", profile(), clock)
    await governor.acquire("embedding", 100, 110, guard)
    ledger = ExperimentLedger(tmp_path / "ledger.db", 5, 100000)
    provider = FakeModels()
    session = ModelSession(provider, Budget(110, clock=clock.monotonic), guard, ledger, "cancel", governor)
    task = asyncio.create_task(session.embed(["query"]))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not provider.calls and session.started_calls == ledger.summary()["calls"] == 0
    ledger.close()
    governor.close()


async def test_independent_usage_axes_and_input_only_price_summary(tmp_path):
    from helpdesk.application.eval_reporting import fee_summary, validate_prices

    ledger = ExperimentLedger(tmp_path / "ledger.db", 5, 10000)
    key = await ledger.start_attempt("r", "embedding", 10)
    await ledger.end_attempt(key, "completed", Usage(17, None))
    assert ledger.summary()["input_tokens"] == 17 and ledger.summary()["output_tokens"] is None
    prices = validate_prices(
        {
            "version": "synthetic",
            "currency": "CNY",
            "chat": {"input_per_million": 2, "output_per_million": 8},
            "embedding": {"input_per_million": 0.5},
            "rerank": {"input_per_million": 0.5},
        }
    )
    assert fee_summary(ledger, ["r"], prices)["fee"] == 0.0000085
    second = await ledger.start_attempt("r", "scope", 10)
    await ledger.end_attempt(second, "error", Usage())
    summary = fee_summary(ledger, ["r"], prices)
    assert summary["fee"] is None and summary["unpriced_attempts"] == 1 and summary["known_fee_estimate"] > 0
    ledger.close()


async def test_invalid_answer_keeps_observed_provider_usage(tmp_path):
    ledger = ExperimentLedger(tmp_path / "ledger.db", 5, 100000)
    body = {
        "choices": [{"finish_reason": "length", "message": {"content": "not-json"}}],
        "usage": {"prompt_tokens": 17, "completion_tokens": 5},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    ) as client:
        import time

        session = ModelSession(
            HttpModels(settings(), client), Budget(time.monotonic() + 5), guard, ledger, "bad"
        )
        with pytest.raises(AppError):
            await session.structured("scope", "system", {})
    assert ledger.summary()["input_tokens"] == 17 and ledger.summary()["output_tokens"] == 5
    assert session.attempt_details[0]["status"] == "error"
    assert session.summary()["known_input_tokens"] == 17
    ledger.close()
