import argparse
import asyncio
import hashlib
import json
import os
import platform
import time
from collections.abc import AsyncIterator
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from importlib.metadata import version
from multiprocessing import get_context
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from helpdesk.adapters.models.client import model_client
from helpdesk.adapters.models.gateway import HttpModels
from helpdesk.adapters.postgres.corpus import PostgresCorpus
from helpdesk.adapters.postgres.uow import PostgresUnitOfWork
from helpdesk.application.adjudication import adjudicate
from helpdesk.application.eval_metrics import coverage_metrics, retrieval_summary
from helpdesk.application.eval_reporting import fee_summary, paired_retrieval, validate_prices
from helpdesk.application.evaluation import (
    EvalCase,
    ExperimentLedger,
    blind_packet,
    code_hash,
    covered,
    load_cases,
    percentile,
    plan,
    write_new,
)
from helpdesk.application.identity import digest_token
from helpdesk.application.model_session import Budget, ModelSession
from helpdesk.application.rebinding import verify_frozen_rebinding, verify_rebinding_approval
from helpdesk.application.releases import canonical_hash, prepare_release
from helpdesk.application.scheduling import RateGovernor, RateProfile
from helpdesk.domain.errors import AppError
from helpdesk.domain.models import Principal, Row
from helpdesk.domain.rag import BoundScope, fingerprint
from helpdesk.entrypoints.runtime import run as run_async
from helpdesk.graph.baseline import PROMPT_HASHES, Context, GraphRunner
from helpdesk.ports.chat import ChatRunInput
from helpdesk.settings import Settings

PACKAGE = Path(__file__).resolve().parents[1]
EVAL_PROTOCOL = {
    "authorization": "frozen-input-permissions-v1",
    "retry": "bounded-retry-after-v1",
    "trace": "observed-stages-v1",
    "usage": "independent-axes-v1",
}


def scope_identity(scope: BoundScope) -> str:
    """实验输入身份不包含无关的全局授权版本；原始版本仍保存在 manifest。"""
    return fingerprint({"kb_ids": scope.kb_ids, "knowledge_bases": scope.knowledge_bases,
                        "versions": scope.version_ids, "snapshots": scope.snapshots})


def restore_scope(value: Row) -> BoundScope:
    return BoundScope(tuple(value["kb_ids"]), tuple(value["knowledge_bases"]),
                      tuple(value["version_ids"]), tuple(value["snapshots"]),
                      value["revision"], value["scope_hash"])


async def check_frozen_input(
    corpus: PostgresCorpus, current: Principal, scope: BoundScope, expected_hash: str,
) -> None:
    # 评测不共享 API 的进程锁；短暂持有授权行共享锁，避免一次复核读到半新半旧策略。
    async with corpus.pool.connection() as conn:
        await conn.execute("SELECT revision FROM authorization_state WHERE id=1 FOR SHARE")
        await _compare_frozen_input(corpus, current, scope, expected_hash)


async def _compare_frozen_input(
    corpus: PostgresCorpus, current: Principal, scope: BoundScope, expected_hash: str,
) -> None:
    await corpus.check(current, scope)
    live = await corpus.bind(current, scope.kb_ids, scope.revision)
    # 增权不扩大冻结输入；只比较原始候选范围内的身份和版本。
    bounded = replace(
        live, kb_ids=scope.kb_ids,
        knowledge_bases=tuple(k for k in live.knowledge_bases if k["id"] in scope.kb_ids),
        version_ids=tuple(v for v in live.version_ids if v in scope.version_ids),
        snapshots=tuple(s for s in live.snapshots if s["kb_id"] in scope.kb_ids),
    )
    if scope_identity(bounded) != scope_identity(scope) or fingerprint(
        await corpus.export(current, scope)
    ) != expected_hash:
        raise AppError("evaluation_input_changed", 409, "冻结评测输入已变化")


def validate_rate_profile(value: Row, settings: Settings) -> RateProfile:
    profile = RateProfile.model_validate(value)
    if profile.endpoints_hash != fingerprint(
        [settings.chat_url, settings.embedding_url, settings.rerank_url]
    ):
        raise ValueError("调度配置端点身份不一致")
    if any(profile.routes[k].upstream != getattr(settings, k + "_model") for k in profile.routes):
        raise ValueError("调度配置上游模型身份不一致")
    if any(
        profile.routes[k].request_model
        != (getattr(settings, k + "_request_model") or getattr(settings, k + "_model"))
        for k in profile.routes
    ):
        raise ValueError("调度配置请求别名不一致")
    return profile


@asynccontextmanager
async def dependencies(
    settings: Settings,
) -> AsyncIterator[
    tuple[AsyncConnectionPool[psycopg.AsyncConnection[Row]], PostgresCorpus, PostgresUnitOfWork]
]:
    pool: AsyncConnectionPool[psycopg.AsyncConnection[Row]] = AsyncConnectionPool(
        settings.database_url.get_secret_value(),
        open=False,
        min_size=1,
        max_size=4,
        timeout=5,
        kwargs={"row_factory": dict_row, "options": "-c statement_timeout=5000"},
    )
    async with pool:
        await pool.wait(timeout=5)
        yield pool, PostgresCorpus(pool, settings), PostgresUnitOfWork(pool)


def model_identity(settings: Settings) -> Row:
    return {
        "chat": settings.chat_model,
        "embedding": settings.embedding_model,
        "dimension": settings.embedding_dimension,
        "rerank": settings.rerank_model,
        "protocol": settings.rerank_format,
        "endpoints_hash": fingerprint([settings.chat_url, settings.embedding_url, settings.rerank_url]),
        "timeout": settings.request_timeout,
        "request_calls": settings.request_calls,
        "token_estimate_limit": settings.token_reservation_limit,
        "request_models": {
            kind: getattr(settings, kind + "_request_model") or getattr(settings, kind + "_model")
            for kind in ("chat", "embedding", "rerank")
        },
        "ca_sha256": hashlib.sha256(settings.model_ca_file.read_bytes()).hexdigest()
        if settings.model_ca_file
        else None,
        "token_estimator": "utf8-bytes-plus-max-output-v1",
        "connect_host": settings.model_connect_host,
        "inference_enabled": settings.model_inference_enabled,
    }


def runtime_identity() -> Row:
    return {
        "python": platform.python_version(),
        "platform": platform.system(),
        "dependencies": {
            name: version(name)
            for name in ("langgraph", "langchain-core", "jieba", "rank-bm25", "httpx", "psycopg", "pydantic")
        },
    }


async def identity(uow: PostgresUnitOfWork) -> tuple[Principal, int]:
    token = os.environ.get("HD_EVAL_TOKEN", "")
    if not token:
        raise ValueError("先显式设置独立评测主体的 HD_EVAL_TOKEN，不从线上会话猜测身份")
    async with uow() as repos:
        principal = await repos.identity.principal(digest_token(token))
        revision = await repos.access.revision()
    if principal is None:
        raise ValueError("评测主体未登录或已失效")
    return principal, revision


async def freeze(args: argparse.Namespace, settings: Settings) -> None:
    rate_path = getattr(args, "rate_profile", None)
    rate_profile = (
        validate_rate_profile(json.loads(rate_path.read_text(encoding="utf-8")), settings)
        if rate_path
        else None
    )
    prices_path = getattr(args, "prices", None)
    prices = validate_prices(json.loads(prices_path.read_text(encoding="utf-8"))) if prices_path else None
    approval_hash = verify_rebinding_approval(
        args.cases, getattr(args, "source_corpus", None), getattr(args, "rebind_approval", None)
    )
    all_cases = load_cases(args.cases)
    split = getattr(args, "split", "dev")
    cases = [case for case in all_cases if case.split == split]
    if not cases:
        raise ValueError("所选划分没有题目")
    source_path = getattr(args, "source_corpus", None)
    if any(case.source for case in cases) and source_path is None:
        raise ValueError("转换的发布题必须显式绑定 --source-corpus")
    source_bundle = json.loads(source_path.read_text(encoding="utf-8")) if source_path else None
    strategies, variants = args.strategies.split(","), args.variants.split(",")
    if not set(strategies).issubset({"baseline", "split-v1"}) or not set(variants).issubset(set("ABCD")):
        raise ValueError("不支持的策略或变体")
    if len(strategies) != len(set(strategies)) or len(variants) != len(set(variants)):
        raise ValueError("实验组不能重复")
    if variants != ["D"] and any(c.scope_mode == "default" or len(c.kb_ids) != 1 for c in cases):
        raise ValueError("A/B/C/D 协议仅用于单库；跨库使用 D 的 baseline/split-v1")
    scopes, bindings, chunks = {}, {}, {}
    async with dependencies(settings) as (pool, corpus, uow):
        principal, revision = await identity(uow)
        for case in cases:
            scope = await corpus.bind(principal, tuple(case.kb_ids), revision)
            exported = await corpus.export(principal, scope)
            if source_bundle is not None:
                verify_source(case, source_bundle, scope, exported, principal.user_id, revision)
            for group in case.evidence_groups:
                if any(not covered(anchor, exported) for anchor in group):
                    raise ValueError(f"题目 {case.id} 的黄金出处无法在当前授权切片中完整核对")
            scopes[case.id] = scope_identity(scope)
            bindings[case.id] = asdict(scope)
            chunks[case.id] = exported
    # 目录只在全部输入校验通过后独占创建，禁止覆盖已有实验和旧报告。
    args.out.mkdir(parents=True, exist_ok=False)
    cases_data = [c.model_dump() for c in cases]
    write_new(args.out / "cases.json", cases_data)
    write_new(args.out / "corpus.json", chunks)
    if approval_hash:
        # 将真实审批字节保留到冻结目录，后续执行继续核对，避免只留下口头确认。
        (args.out / "rebind-approval.json").write_bytes(args.rebind_approval.read_bytes())
        (args.out / "rebind-review-packet.json").write_bytes(
            (args.cases.parent / "review-packet.json").read_bytes()
        )
    manifest = {
        "schema": "rewrite-eval-v2",
        "protocol": EVAL_PROTOCOL,
        "rate_profile_hash": rate_profile.digest() if rate_profile else None,
        "prices_hash": fingerprint(prices) if prices else None,
        "split": split,
        "source_scope_version": source_bundle["scope_version"] if source_bundle else None,
        "rebind_approval_sha256": approval_hash,
        "subject_id": principal.user_id,
        "subject_role": principal.role,
        "authorization_revision": revision,
        "scopes": scopes,
        "scope_bindings": bindings,
        "cases_hash": fingerprint(cases_data),
        "corpus_hash": fingerprint(chunks),
        "code_hash": code_hash(PACKAGE),
        "prompt_hashes": PROMPT_HASHES,
        "models": model_identity(settings),
        "runtime": runtime_identity(),
        "seed": args.seed,
        "max_calls": args.max_calls,
        "reservation_limit_estimate": args.reservation_limit,
        "plan": plan(cases, strategies, variants, args.seed),
    }
    write_new(args.out / "manifest.json", manifest)
    if rate_profile:
        write_new(args.out / "rate-profile.json", rate_profile.model_dump(by_alias=True))
    if prices:
        write_new(args.out / "prices.json", prices)
    print(
        json.dumps({"status": "frozen", "cases": len(cases), "runs": len(manifest["plan"]), "model_calls": 0})
    )


def verify_source(
    case: "EvalCase", bundle: Row, scope: BoundScope, chunks: list[Row], user_id: str, revision: int
) -> None:
    # 只对照已审原文；不能把黄金事实传给检索或模型。
    if bundle["scope_version"] != canonical_hash({k: v for k, v in bundle.items() if k != "scope_version"}):
        raise ValueError("来源语料哈希无效")
    if (
        case.source.get("scope_version") != bundle["scope_version"]
        or user_id != bundle["principal"]["user_id"]
        or revision != bundle["principal"]["revision"]
    ):
        raise ValueError("原来源与当前主体/权限不一致")
    members = (
        [m for m in bundle["members"] if m["kb_id"] in case.kb_ids]
        if case.scope_mode == "selected"
        else [m for m in bundle["members"] if m["default_search"]]
    )
    if (
        set(scope.kb_ids) != {m["kb_id"] for m in members}
        or set(scope.version_ids) != {v for m in members for v in m["version_ids"]}
        or {s["kb_id"]: s["id"] for s in scope.snapshots} != {m["kb_id"]: m["snapshot_id"] for m in members}
    ):
        raise ValueError("当前范围/版本/快照与已审核来源不同，禁止生成")
    keys: tuple[str, ...] = (
        "id",
        "version_id",
        "kb_id",
        "title",
        "text",
        "locator",
        "content_hash",
        "organization",
    )
    if all("vector_hash" in c for c in bundle["corpus"]["chunks"]):
        keys += ("vector_hash",)
    expected = [
        {**{k: c[k] for k in keys}, "kb_name": c["knowledge_base_name"]}
        for c in bundle["corpus"]["chunks"]
        if c["version_id"] in scope.version_ids
    ]
    actual = [{k: c[k] for k in (*keys, "kb_name")} for c in chunks]
    if canonical_hash(sorted(expected, key=lambda c: c["id"])) != canonical_hash(
        sorted(actual, key=lambda c: c["id"])
    ):
        raise ValueError("当前原文与已审核来源不同")


async def execute(args: argparse.Namespace, settings: Settings) -> None:
    from helpdesk.application.evaluation import EvalCase

    folder = args.directory
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("rebind_approval_sha256"):
        verify_frozen_rebinding(folder, manifest["rebind_approval_sha256"])
    data = json.loads((folder / "cases.json").read_text(encoding="utf-8"))
    chunks = json.loads((folder / "corpus.json").read_text(encoding="utf-8"))
    if (
        manifest["schema"] != "rewrite-eval-v2"
        or manifest.get("protocol") != EVAL_PROTOCOL
        or manifest["cases_hash"] != fingerprint(data)
        or manifest["corpus_hash"] != fingerprint(chunks)
        or manifest["code_hash"] != code_hash(PACKAGE)
        or manifest["models"] != model_identity(settings)
        or manifest["runtime"] != runtime_identity()
        or manifest["prompt_hashes"] != PROMPT_HASHES
    ):
        raise ValueError("代码、模型或冻结输入漂移；需创建新实验，禁止续接")
    rate_profile = None
    if manifest.get("rate_profile_hash"):
        rate_profile = validate_rate_profile(
            json.loads((folder / "rate-profile.json").read_text(encoding="utf-8")), settings
        )
        if rate_profile.digest() != manifest["rate_profile_hash"]:
            raise ValueError("调度配置漂移")
    if args.execute and rate_profile is None:
        raise ValueError("真实执行必须冻结明确的 --rate-profile")
    if (
        manifest.get("prices_hash")
        and fingerprint(json.loads((folder / "prices.json").read_text(encoding="utf-8")))
        != manifest["prices_hash"]
    ):
        raise ValueError("冻结价格漂移")
    cases = {case.id: case for case in [EvalCase.model_validate(row) for row in data]}
    started_marker = folder / "execution-started.json"
    if started_marker.exists():
        recorded = json.loads(started_marker.read_text(encoding="utf-8"))
        if recorded.get("manifest_hash") != fingerprint(manifest) or any(
            not (folder / name).exists() for name in ("ledger.sqlite", "scheduling.sqlite")
        ):
            raise ValueError("运行状态缺失或身份漂移，禁止重建预算/调度状态")
    # 文件锁存在表示前一执行状态不明，人工核对后才能清理，防止双进程花费。
    lock = folder / "execution.lock"
    with lock.open("x", encoding="utf-8") as stream:
        stream.write(str(os.getpid()))
    ledger = ExperimentLedger(
        folder / "ledger.sqlite", manifest["max_calls"], manifest["reservation_limit_estimate"]
    )
    cpu = ProcessPoolExecutor(max_workers=2, mp_context=get_context("spawn"))
    governor = None
    try:
        async with (
            dependencies(settings) as (pool, corpus, uow),
            model_client(settings) as client,
        ):
            principal, revision = await identity(uow)
            if (principal.user_id != manifest["subject_id"]
                    or principal.role != manifest["subject_role"]):
                raise ValueError("评测主体变化，停止运行")
            scopes = {}
            for case in cases.values():
                scope = restore_scope(manifest["scope_bindings"][case.id])
                if scope_identity(scope) != manifest["scopes"][case.id]:
                    raise ValueError("冻结范围记录不一致")
                await check_frozen_input(corpus, principal, scope, fingerprint(chunks[case.id]))
                scopes[case.id] = scope
            if not args.execute:
                print(
                    json.dumps(
                        {"status": "validated", "planned_runs": len(manifest["plan"]), "model_calls": 0}
                    )
                )
                return
            if not settings.models_ready:
                raise ValueError("模型未完整配置")
            assert rate_profile is not None
            if not started_marker.exists():
                if ledger.summary()["calls"] or (folder / "scheduling.sqlite").exists():
                    raise ValueError("已有状态未绑定启动记录，需人工核对")
                write_new(started_marker, {"manifest_hash": fingerprint(manifest)})
            governor = RateGovernor(
                folder / "scheduling.sqlite", rate_profile, require_existing=bool(ledger.summary()["calls"])
            )
            done = {row["key"] for row in ledger.results()}
            # 进程中断时已付费但没有结果的运行，不会在续跑时自动重复调用。
            started = {row[0] for row in ledger.conn.execute("SELECT DISTINCT run_id FROM attempts")}
            for arm in manifest["plan"]:
                key = arm["key"]
                if key in done:
                    continue
                if key in started:
                    ledger.save(
                        key,
                        {
                            **arm,
                            "status": "interrupted",
                            "stop_reason": "previous_run_incomplete",
                            "elapsed_ms": None,
                            "server_ttft_ms": None,
                            "calls": None,
                            "answer": "",
                            "citations": [],
                        },
                    )
                    continue
                case, scope = cases[arm["case_id"]], scopes[arm["case_id"]]

                async def guard(bound: BoundScope = scope, expected: str = fingerprint(chunks[case.id])) -> None:
                    try:
                        async with asyncio.timeout(5):
                            current, _ = await identity(uow)
                            if current.user_id != principal.user_id or current.role != principal.role:
                                raise AppError("authorization_changed", 409, "评测主体已变化")
                            await check_frozen_input(corpus, current, bound, expected)
                    except ValueError as error:
                        raise AppError("authorization_changed", 409, "评测身份已失效") from error
                    except AppError:
                        raise
                    except Exception as error:
                        raise AppError("authorization_check_failed", 503, "评测权限检查失败") from error

                started_at = time.monotonic()
                first_text = None
                session = ModelSession(
                    HttpModels(settings, client),
                    Budget(
                        started_at + settings.request_timeout,
                        settings.request_calls,
                        settings.token_reservation_limit,
                    ),
                    guard,
                    ledger,
                    key,
                    governor,
                )
                run = ChatRunInput(
                    key,
                    key,
                    principal,
                    revision,
                    tuple(case.kb_ids),
                    case.question,
                    tuple(case.history),
                    arm["strategy"],
                    session.budget.deadline,
                )
                runner = GraphRunner(
                    Context(run, scope, session, corpus, cpu, asyncio.Semaphore(2), arm["variant"])
                )
                result: Row = {**arm, "answer": "", "citations": [], "server_ttft_ms": None}
                cancelled = False
                try:
                    async with asyncio.timeout(settings.request_timeout):
                        async for event in runner.events():
                            if event.event == "answer_delta" and first_text is None:
                                first_text = (time.monotonic() - started_at) * 1000
                    await guard()
                    if runner.outcome is None:
                        raise ValueError("图没有终态")
                    outcome = runner.outcome
                    result.update(
                        status=outcome.status,
                        answer=outcome.answer,
                        citations=outcome.citations,
                        trace=outcome.trace,
                        server_ttft_ms=first_text,
                    )
                except asyncio.CancelledError:
                    cancelled = True
                    result.update(status="interrupted", stop_reason="interrupted")
                except Exception as error:
                    result.update(
                        status="error",
                        stop_reason=error.code
                        if isinstance(error, AppError)
                        else "deadline"
                        if isinstance(error, TimeoutError)
                        else "execution_failed",
                    )
                trace = runner.diagnostics()
                result.update(
                    trace=trace,
                    attempt_details=session.attempt_details,
                    elapsed_ms=round((time.monotonic() - started_at) * 1000),
                    server_ttft_ms=first_text,
                    **session.summary(),
                    **coverage_metrics(case, trace, chunks[case.id]),
                )
                ledger.save(key, result)
                if cancelled:
                    raise asyncio.CancelledError
                if result.get("stop_reason") in {
                    "experiment_budget_exhausted",
                    "authorization_changed",
                    "authorization_check_failed",
                    "evaluation_input_changed",
                    "rate_clock_changed",
                    "rate_state_invalid",
                }:
                    break
    finally:
        if governor is not None:
            governor.close()
        ledger.close()
        lock.unlink(missing_ok=True)
        await asyncio.to_thread(cpu.shutdown, wait=True, cancel_futures=True)


def report(args: argparse.Namespace) -> None:
    from helpdesk.application.evaluation import EvalCase

    manifest = json.loads((args.directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest["schema"] != "rewrite-eval-v2":
        raise ValueError("旧协议报告保持原工具和原字节，禁止用新口径覆盖")
    prices = None
    if manifest.get("prices_hash"):
        prices = validate_prices(json.loads((args.directory / "prices.json").read_text(encoding="utf-8")))
        if fingerprint(prices) != manifest["prices_hash"]:
            raise ValueError("冻结价格漂移")
    cases = [
        EvalCase.model_validate(row)
        for row in json.loads((args.directory / "cases.json").read_text(encoding="utf-8"))
    ]
    ledger = ExperimentLedger(
        args.directory / "ledger.sqlite", manifest["max_calls"], manifest["reservation_limit_estimate"]
    )
    try:
        results = ledger.results()
        packet, mapping = blind_packet(cases, results, manifest["seed"])
        summaries = []
        for strategy, variant in sorted({(p["strategy"], p["variant"]) for p in manifest["plan"]}):
            rows = [r for r in results if r["strategy"] == strategy and r["variant"] == variant]
            expected = sum(p["strategy"] == strategy and p["variant"] == variant for p in manifest["plan"])
            latency = [r["elapsed_ms"] for r in rows if r.get("elapsed_ms") is not None]
            summaries.append(
                {
                    "strategy": strategy,
                    "variant": variant,
                    "planned": expected,
                    "recorded": len(rows),
                    "pending": expected - len(rows),
                    "statuses": {s: sum(r["status"] == s for r in rows) for s in {r["status"] for r in rows}},
                    "latency_all_recorded_p50": percentile(latency, 0.5),
                    "latency_all_recorded_p95": percentile(latency, 0.95),
                    "server_ttft_ms": {
                        "n": sum(r.get("server_ttft_ms") is not None for r in rows),
                        "p50": percentile(
                            [r["server_ttft_ms"] for r in rows if r.get("server_ttft_ms") is not None], 0.5
                        ),
                        "p95": percentile(
                            [r["server_ttft_ms"] for r in rows if r.get("server_ttft_ms") is not None], 0.95
                        ),
                    },
                    "model_usage": ledger.group_usage(
                        [
                            p["key"]
                            for p in manifest["plan"]
                            if p["strategy"] == strategy and p["variant"] == variant
                        ]
                    ),
                    "retrieval": retrieval_summary(rows, {c.id: c for c in cases}),
                    "model_wait_ms_total": sum(r.get("model_wait_ms", 0) for r in rows),
                    "split_attempted": sum(
                        any(a["stage"] == "split" for a in r.get("attempt_details", [])) for r in rows
                    ),
                    "split_accepted": sum(
                        r.get("trace", {}).get("split", {}).get("accepted") is True for r in rows
                    ),
                    **fee_summary(ledger, [r["key"] for r in rows], prices),
                    "latency_by_status": {
                        s: {
                            "n": sum(r["status"] == s for r in rows),
                            "p95": percentile(
                                [
                                    r["elapsed_ms"]
                                    for r in rows
                                    if r["status"] == s and r.get("elapsed_ms") is not None
                                ],
                                0.95,
                            ),
                        }
                        for s in {r["status"] for r in rows}
                    },
                    "human_correctness": None,
                    "false_refusal": None,
                    "unsupported": None,
                    "correct_to_wrong": None,
                }
            )
        args.out.mkdir(parents=True, exist_ok=False)
        write_new(args.out / "blind-review.json", packet)
        write_new(args.out / "private-mapping.json", mapping)
        write_new(args.out / "results.json", results)
        write_new(args.out / "retrieval-pairs.json", paired_retrieval(results))
        write_new(
            args.out / "summary.json",
            {
                "groups": summaries,
                "generation_ledger": ledger.summary(),
                "judge_calls": 0,
                "note": "替身不证明质量；人工裁决前正确率为 null，未启用回答修正时误改率为 null。",
            },
        )
    finally:
        ledger.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="新图独立评测，冻结和验证默认零模型调用")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare-scope")
    prepare_parser.add_argument("source", type=Path)
    prepare_parser.add_argument("--out", type=Path, required=True)
    freeze_parser = sub.add_parser("freeze")
    freeze_parser.add_argument("cases", type=Path)
    freeze_parser.add_argument("--out", type=Path, required=True)
    freeze_parser.add_argument("--strategies", default="baseline,split-v1")
    freeze_parser.add_argument("--variants", default="D")
    freeze_parser.add_argument("--seed", type=int, default=20261017)
    freeze_parser.add_argument("--split", choices=["dev", "test"], default="dev")
    freeze_parser.add_argument("--source-corpus", type=Path)
    freeze_parser.add_argument("--rebind-approval", type=Path)
    freeze_parser.add_argument("--rate-profile", type=Path)
    freeze_parser.add_argument("--prices", type=Path)
    freeze_parser.add_argument("--max-calls", type=int, required=True)
    freeze_parser.add_argument("--reservation-limit", type=int, required=True)
    execute_parser = sub.add_parser("run")
    execute_parser.add_argument("directory", type=Path)
    execute_parser.add_argument("--execute", action="store_true", help="实际模型调用；需事先批准费用预算")
    report_parser = sub.add_parser("report")
    report_parser.add_argument("directory", type=Path)
    report_parser.add_argument("--out", type=Path, required=True)
    adjudicate_parser = sub.add_parser("adjudicate")
    adjudicate_parser.add_argument("directory", type=Path)
    adjudicate_parser.add_argument("--decisions", type=Path, required=True)
    adjudicate_parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare-scope":
        print(json.dumps(prepare_release(args.source, args.out), ensure_ascii=False))
        return
    if args.command == "adjudicate":
        adjudicate(args.directory, args.decisions, args.out)
        return
    if args.command == "report":
        report(args)
        return
    if args.command == "freeze" and (args.max_calls < 1 or args.reservation_limit < 1):
        parser.error("实验总预算必须为正数")
    settings = Settings()  # type: ignore[call-arg]
    run_async(freeze(args, settings) if args.command == "freeze" else execute(args, settings))


if __name__ == "__main__":
    main()
