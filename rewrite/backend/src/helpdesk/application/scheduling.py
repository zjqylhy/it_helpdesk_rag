"""单实验的持久准入窗口；网关仍是跨应用配额的权威。"""

import asyncio
import json
import math
import sqlite3
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from helpdesk.domain.errors import AppError
from helpdesk.domain.rag import fingerprint

RATE_ESTIMATOR = "utf8-wire-framing-and-repeated-query-v1"


class RouteLimit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    quota_key: str = Field(min_length=1, max_length=200)
    provider_id: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=200)
    upstream: str = Field(min_length=1, max_length=200)
    request_model: str = Field(min_length=1, max_length=200)
    rpm: int = Field(gt=0, le=1000000)
    units_per_minute: int = Field(gt=0, le=10**10)
    candidate_count: Literal[1]
    gray_percent: Literal[0]


class RateProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)
    schema_version: Literal["rewrite-rate-profile-v1"] = Field(alias="schema")
    gateway_version: int = Field(gt=0)
    endpoints_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    estimator: Literal["utf8-wire-framing-and-repeated-query-v1"] = "utf8-wire-framing-and-repeated-query-v1"
    routes: dict[str, RouteLimit]

    @model_validator(mode="after")
    def complete(self) -> "RateProfile":
        if set(self.routes) != {"chat", "embedding", "rerank"}:
            raise ValueError("调度配置必须覆盖三个路由")
        quotas: dict[str, tuple] = {}
        models: dict[tuple, str] = {}
        for route in self.routes.values():
            identity = (route.provider_id, route.upstream)
            limits = (*identity, route.rpm, route.units_per_minute)
            if quotas.setdefault(route.quota_key, limits) != limits:
                raise ValueError("同一配额键的模型或限额不一致")
            if models.setdefault(identity, route.quota_key) != route.quota_key:
                raise ValueError("同一上游不能分配多个配额键")
        return self

    def digest(self) -> str:
        return fingerprint(self.model_dump(by_alias=True))


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def utc(self) -> float: ...
    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    monotonic = staticmethod(time.monotonic)
    utc = staticmethod(time.time)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def route_kind(stage: str) -> str:
    return stage if stage in {"embedding", "rerank"} else "chat"


def rate_units(stage: str, payload: object, output: int) -> int:
    size = len(json.dumps(payload, ensure_ascii=False).encode()) + output + 1024
    if stage == "rerank" and isinstance(payload, dict):
        # 上游按 Query/Document 配对计入输入，不能只预留一次 Query。
        size += max(0, len(payload["documents"]) - 1) * len(payload["query"].encode())
    return size


class RateGovernor:
    def __init__(
        self, path: Path, profile: RateProfile, clock: Clock | None = None, *, require_existing: bool = False
    ) -> None:
        self.clock = clock or SystemClock()
        self.profile = profile
        self.conn = sqlite3.connect(path)
        self.conn.executescript("""CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE IF NOT EXISTS admissions(quota TEXT,at REAL,units INTEGER);
          CREATE INDEX IF NOT EXISTS admission_window ON admissions(quota,at);
          CREATE TABLE IF NOT EXISTS cooldowns(quota TEXT PRIMARY KEY,until REAL);""")
        existing = dict(self.conn.execute("SELECT key,value FROM metadata"))
        if require_existing and not existing:
            self.conn.close()
            raise ValueError("已有调用的调度状态不可重新初始化")
        if existing and existing.get("profile") != profile.digest():
            self.conn.close()
            raise ValueError("调度状态的配置身份不匹配")
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO metadata VALUES('profile',?)", (profile.digest(),))
        self.locks = {route.quota_key: asyncio.Lock() for route in profile.routes.values()}
        self.last_wall = self.clock.utc()
        self.last_mono = self.clock.monotonic()
        self._now()

    def close(self) -> None:
        self.conn.close()

    def _now(self) -> float:
        wall, mono = self.clock.utc(), self.clock.monotonic()
        expected = self.last_wall + mono - self.last_mono
        stored = self.conn.execute("SELECT value FROM metadata WHERE key='last_wall'").fetchone()
        if stored is not None and not math.isfinite(float(stored[0])):
            raise AppError("rate_state_invalid", 503, "调度时钟记录异常")
        if (
            not math.isfinite(wall)
            or wall < expected - 1
            or (stored is not None and wall < float(stored[0]) - 1)
        ):
            raise AppError("rate_clock_changed", 503, "调度时钟变化，停止发送")
        # 进程内不因系统时钟前跳提前释放窗口；UTC 仅用于持久恢复。
        now = expected
        if stored is not None and now < float(stored[0]) - 1:
            raise AppError("rate_clock_changed", 503, "调度时钟变化，停止发送")
        self.last_wall, self.last_mono = now, mono
        return now

    async def acquire(
        self, kind: str, units: int, deadline: float, guard: Callable[[], Awaitable[None]]
    ) -> float:
        try:
            return await self._acquire(kind, units, deadline, guard)
        except (sqlite3.DatabaseError, ValueError, TypeError, OverflowError) as error:
            raise AppError("rate_state_invalid", 503, "调度状态异常，停止发送") from error

    async def _acquire(
        self, kind: str, units: int, deadline: float, guard: Callable[[], Awaitable[None]]
    ) -> float:
        route = self.profile.routes[kind]
        if units > route.units_per_minute or units < 1:
            raise AppError("rate_request_too_large", 429, "单次请求超过调度窗口容量")
        started = self.clock.monotonic()
        while True:
            await guard()
            remaining = deadline - self.clock.monotonic()
            if remaining <= 0:
                raise AppError("model_rate_deadline", 504, "模型调度等待超过请求时限")
            async with self.locks[route.quota_key]:
                now = self._now()
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    rows = self.conn.execute(
                        "SELECT at,units FROM admissions WHERE quota=? AND at>? ORDER BY at",
                        (route.quota_key, now - 60),
                    ).fetchall()
                    cooldown = self.conn.execute(
                        "SELECT until FROM cooldowns WHERE quota=?", (route.quota_key,)
                    ).fetchone()
                    if any(
                        type(u) is not int
                        or u <= 0
                        or u > route.units_per_minute
                        or not math.isfinite(at)
                        or at > now + 1
                        for at, u in rows
                    ):
                        raise ValueError("调度窗口记录无效")
                    if cooldown and (not math.isfinite(cooldown[0]) or cooldown[0] < 0):
                        raise ValueError("冷却记录无效")
                    due = max(now, cooldown[0] if cooldown else now)
                    if rows:
                        due = max(due, rows[-1][0] + 60 / route.rpm)
                    count, used = len(rows), sum(row[1] for row in rows)
                    for at, reserved in rows:
                        if count < route.rpm and used + units <= route.units_per_minute:
                            break
                        due = max(due, at + 60)
                        count, used = count - 1, used - reserved
                    delay = max(0.0, due - now)
                    if delay <= 1e-8:
                        self.conn.execute(
                            "INSERT INTO admissions VALUES(?,?,?)", (route.quota_key, now, units)
                        )
                        self.conn.execute("DELETE FROM admissions WHERE at<=?", (now - 60,))
                        self.conn.execute(
                            "INSERT OR REPLACE INTO metadata VALUES('last_wall',?)", (str(now),)
                        )
                        return self.clock.monotonic() - started
            if delay >= remaining:
                raise AppError("model_rate_deadline", 504, "模型调度等待超过请求时限")
            # 等待不持有其他路由的锁，并定期检查取消和当前权限。
            await self.clock.sleep(min(delay, 1.0))

    async def cool_down(self, kind: str, seconds: float) -> None:
        try:
            self._cool_down(kind, seconds)
        except (sqlite3.DatabaseError, ValueError, TypeError, OverflowError) as error:
            raise AppError("rate_state_invalid", 503, "调度状态异常，停止发送") from error

    def _cool_down(self, kind: str, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("冷却时长无效")
        quota = self.profile.routes[kind].quota_key
        now = self._now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO cooldowns VALUES(?,?) ON CONFLICT(quota) DO UPDATE SET until=max(until,excluded.until)",
                (quota, now + seconds),
            )
            self.conn.execute("INSERT OR REPLACE INTO metadata VALUES('last_wall',?)", (str(now),))
