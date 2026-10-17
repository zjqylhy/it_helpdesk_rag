"""离线实验的不可变输入、持久请求预算和人工裁决；不产生自动有效性结论。"""

import hashlib
import json
import math
import random
import sqlite3
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from helpdesk.domain.errors import AppError
from helpdesk.domain.models import Row
from helpdesk.domain.rag import Usage, fingerprint


class Anchor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version_id: str
    paragraph: int = Field(ge=1)
    page: int | None = Field(default=None, ge=1)
    section: str | None = None
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1)

    @model_validator(mode="after")
    def ordered(self) -> "Anchor":
        if self.end <= self.start or self.end - self.start != len(self.quote):
            raise ValueError("锚点区间和节选不一致")
        return self


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=8000)
    kb_ids: list[str] = Field(default_factory=list, max_length=32)
    scope_mode: Literal["default", "selected"] = "selected"
    history: list[Row] = Field(default_factory=list, max_length=8)
    expected_behavior: Literal["answer", "refuse", "clarify"]
    evidence_groups: list[list[Anchor]] = Field(default_factory=list)
    required_facts: list[str] = Field(default_factory=list)
    forbidden_facts: list[str] = Field(default_factory=list)
    theme: str = Field(min_length=1)
    split: Literal["dev", "test"]
    reviewer: str = Field(min_length=1)
    reviewed_at: str = Field(min_length=1)
    fact_topics: list[str] = Field(default_factory=list)
    scene: str = ""
    expected_route_kb_ids: list[str] | None = None
    expected_required_kb_ids: list[str] = Field(default_factory=list)
    source: Row = Field(default_factory=dict)

    @model_validator(mode="after")
    def complete(self) -> "EvalCase":
        if not self.question.strip() or len(set(self.kb_ids)) != len(self.kb_ids):
            raise ValueError("问题或范围无效")
        if (self.scope_mode == "selected") != bool(self.kb_ids):
            raise ValueError("指定范围必须列出知识库，默认范围必须为空")
        if self.expected_behavior == "answer" and (
            not self.evidence_groups
            or any(not group for group in self.evidence_groups)
            or not self.required_facts
        ):
            raise ValueError("可回答题必须具有逐组出处与必要事实")
        if any(
            set(item) != {"role", "content"}
            or item["role"] not in {"user", "assistant"}
            or not isinstance(item["content"], str)
            or len(item["content"]) > 8000
            for item in self.history
        ):
            raise ValueError("历史格式无效")
        return self


def load_cases(path: Path) -> list[EvalCase]:
    cases = [
        EvalCase.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not cases or len({c.id for c in cases}) != len(cases):
        raise ValueError("题目为空或编号重复")
    themes: dict[str, str] = {}
    for case in cases:
        for topic in [case.theme, *case.fact_topics]:
            if themes.setdefault(topic, case.split) != case.split:
                raise ValueError("同一事实主题不能跨开发与测试划分")
    return cases


def write_new(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")


def covered(anchor: Anchor, chunks: list[Row]) -> bool:
    spans = sorted(
        (c["locator"]["start"], c["locator"]["end"], c["text"])
        for c in chunks
        if c["version_id"] == anchor.version_id
        and c["locator"].get("paragraph") == anchor.paragraph
        and c["locator"].get("page") == anchor.page
        and c["locator"].get("section") == anchor.section
    )
    position = anchor.start
    for start, end, text in spans:
        if start > position or end <= position:
            continue
        left, right = max(start, anchor.start), min(end, anchor.end)
        if text[left - start : right - start] != anchor.quote[left - anchor.start : right - anchor.start]:
            continue
        position = max(position, right)
        if position >= anchor.end:
            return True
    return False


def recall(case: EvalCase, ids: list[str], chunks: list[Row]) -> Row:
    selected = [c for c in chunks if c["id"] in set(ids)]
    flags = [any(covered(anchor, selected) for anchor in group) for group in case.evidence_groups]
    return {
        "covered_groups": sum(flags),
        "required_groups": len(flags),
        "recall": sum(flags) / len(flags) if flags else None,
        "full": all(flags) if flags else None,
    }


def plan(cases: list[EvalCase], strategies: list[str], variants: list[str], seed: int) -> list[Row]:
    rng = random.Random(seed)
    order = list(cases)
    rng.shuffle(order)
    rows = []
    for case in order:
        arms = [(s, v) for s in strategies for v in variants]
        rng.shuffle(arms)
        for strategy, variant in arms:
            rows.append(
                {
                    "case_id": case.id,
                    "strategy": strategy,
                    "variant": variant,
                    "key": fingerprint([case.id, strategy, variant]),
                }
            )
    return rows


class ExperimentLedger:
    """SQLite 同步事务仅含小记录，先落请求槽再调用模型；进程锁由入口持有。"""

    def __init__(self, path: Path, max_calls: int, reservation_limit: int) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.executescript("""CREATE TABLE IF NOT EXISTS attempts(
          id TEXT PRIMARY KEY,run_id TEXT,stage TEXT,reserved INTEGER,status TEXT,
          input_tokens INTEGER,output_tokens INTEGER);
          CREATE TABLE IF NOT EXISTS results(key TEXT PRIMARY KEY,payload TEXT NOT NULL);""")
        self.max_calls, self.limit = max_calls, reservation_limit

    def close(self) -> None:
        self.conn.close()

    async def start_attempt(self, run_id: str, stage: str, reserved: int) -> str:
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            count, used = self.conn.execute(
                "SELECT count(*),COALESCE(sum(reserved),0) FROM attempts"
            ).fetchone()
            if count >= self.max_calls or used + reserved > self.limit:
                raise AppError("experiment_budget_exhausted", 429, "全实验预算耗尽，禁止继续调用")
            key = uuid4().hex
            self.conn.execute(
                "INSERT INTO attempts VALUES(?,?,?,?,'started',NULL,NULL)", (key, run_id, stage, reserved)
            )
        return key

    async def end_attempt(self, attempt_id: str, status: str, usage: Usage) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE attempts SET status=?,input_tokens=?,output_tokens=? WHERE id=? AND status='started'",
                (status, usage.input_tokens, usage.output_tokens, attempt_id),
            )

    def save(self, key: str, result: Row) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO results VALUES(?,?)", (key, json.dumps(result, ensure_ascii=False))
            )

    def results(self) -> list[Row]:
        return [json.loads(row[0]) for row in self.conn.execute("SELECT payload FROM results ORDER BY rowid")]

    def summary(self) -> Row:
        count, incoming, outgoing, unknown = (
            self.conn.execute("""SELECT count(*),sum(input_tokens),sum(output_tokens),
            COALESCE(sum(input_tokens IS NULL OR output_tokens IS NULL),0) FROM attempts""").fetchone()
        )
        return {
            "calls": count,
            "input_tokens": incoming if not unknown else None,
            "output_tokens": outgoing if not unknown else None,
            "unknown_usage_calls": unknown,
            "reserved_token_estimate": self.conn.execute(
                "SELECT COALESCE(sum(reserved),0) FROM attempts"
            ).fetchone()[0],
        }

    def group_usage(self, keys: list[str]) -> Row:
        if not keys:
            return {"calls": 0, "by_stage": []}
        placeholders = ",".join("?" for _ in keys)
        rows = self.conn.execute(
            f"""SELECT stage,count(*),sum(input_tokens),sum(output_tokens),
            sum(input_tokens IS NULL OR output_tokens IS NULL),sum(reserved)
            FROM attempts WHERE run_id IN ({placeholders}) GROUP BY stage ORDER BY stage""",
            keys,
        ).fetchall()
        return {
            "calls": sum(r[1] for r in rows),
            "by_stage": [
                {
                    "stage": r[0],
                    "calls": r[1],
                    "input_tokens": r[2] if not r[4] else None,
                    "output_tokens": r[3] if not r[4] else None,
                    "unknown_usage_calls": r[4],
                    "reserved_token_estimate": r[5],
                }
                for r in rows
            ],
        }


def percentile(values: list[float], fraction: float) -> float | None:
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def blind_packet(cases: list[EvalCase], results: list[Row], seed: int) -> tuple[list[Row], Row]:
    rng = random.Random(seed)
    shuffled = list(results)
    rng.shuffle(shuffled)
    by_id = {c.id: c for c in cases}
    public, mapping = [], {}
    for index, result in enumerate(shuffled):
        label = f"R{index + 1:04d}"
        case = by_id[result["case_id"]]
        public.append(
            {
                "review_id": label,
                "case": case.model_dump(),
                "status": result["status"],
                "answer": result.get("answer", ""),
                "citations": result.get("citations", []),
                "correct": None,
                "unsupported": None,
                "omitted_requirements": None,
                "reviewer": "",
                "reason": "",
            }
        )
        mapping[label] = result["key"]
    return public, mapping


def code_hash(package: Path) -> str:
    return fingerprint(
        [
            (str(path.relative_to(package)), hashlib.sha256(path.read_bytes()).hexdigest())
            for path in sorted(package.rglob("*"))
            if path.suffix in {".py", ".json"}
        ]
    )
