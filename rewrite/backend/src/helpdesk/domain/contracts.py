from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EvidenceSpan(Contract):
    chunk_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> "EvidenceSpan":
        if self.end <= self.start:
            raise ValueError("证据区间必须非空且有序")
        return self


class TaskCoverage(Contract):
    task_id: str = Field(min_length=1)
    status: Literal["covered", "partial", "missing", "ambiguous", "unknown"]
    support: list[EvidenceSpan]
    missing_requirements: list[str]
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def consistent(self) -> "TaskCoverage":
        if self.status == "covered" and (not self.support or self.missing_requirements):
            raise ValueError("完整覆盖必须有支持且无缺项")
        if self.status == "missing" and (self.support or not self.missing_requirements):
            raise ValueError("缺失必须列出缺项且不能伪造证据")
        return self


class CoverageReport(Contract):
    schema_version: Literal["coverage-v1"] = Field(alias="schema", default="coverage-v1")
    round: int = Field(ge=0, le=1)
    context_hash: str = Field(min_length=1)
    tasks: list[TaskCoverage] = Field(min_length=1, max_length=2)
    missing_task_ids: list[str]

    @model_validator(mode="after")
    def consistent(self) -> "CoverageReport":
        ids = [t.task_id for t in self.tasks]
        if len(ids) != len(set(ids)) or len(self.missing_task_ids) != len(set(self.missing_task_ids)):
            raise ValueError("任务编号不能重复")
        expected = {t.task_id for t in self.tasks if t.status in {"partial", "missing"}}
        if set(self.missing_task_ids) != expected:
            raise ValueError("缺项编号与任务结果不一致")
        return self


class ClaimCheck(Contract):
    claim_id: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    task_ids: list[str] = Field(min_length=1)
    citation_labels: list[str]
    evidence_spans: list[EvidenceSpan]
    status: Literal["supported", "unsupported", "uncertain"]

    @model_validator(mode="after")
    def consistent(self) -> "ClaimCheck":
        if self.end <= self.start:
            raise ValueError("陈述区间必须非空且有序")
        if self.status == "supported" and (not self.citation_labels or not self.evidence_spans):
            raise ValueError("支持陈述必须映射引用与证据")
        return self


class AnswerCheck(Contract):
    schema_version: Literal["answer-check-v1"] = Field(alias="schema", default="answer-check-v1")
    draft_hash: str = Field(min_length=1)
    context_hash: str = Field(min_length=1)
    claims: list[ClaimCheck] = Field(min_length=1)
    missing_task_ids: list[str]
    unsupported_claim_ids: list[str]
    invalid_citation_labels: list[str]
    verdict: Literal["pass", "repair", "insufficient", "unknown"]

    @model_validator(mode="after")
    def consistent(self) -> "AnswerCheck":
        ids = [c.claim_id for c in self.claims]
        unsupported = {c.claim_id for c in self.claims if c.status == "unsupported"}
        if len(ids) != len(set(ids)) or set(self.unsupported_claim_ids) != unsupported:
            raise ValueError("关键陈述编号不一致")
        if self.verdict == "pass" and (
            self.missing_task_ids
            or unsupported
            or self.invalid_citation_labels
            or any(c.status != "supported" for c in self.claims)
        ):
            raise ValueError("存在未解决问题，不能放行")
        return self


class ChatRequest(Contract):
    session_id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=8000)
    request_id: str = Field(min_length=1, max_length=200)


class PublicEvent(Contract):
    event: Literal["status", "evidence", "answer_delta", "citations", "refusal", "done", "error"]
    request_id: str
    message_id: str
    seq: int = Field(ge=1)
    # 各事件的字段投影由 M2 EventSink 实现；此处只冻结包络。
    data: dict[str, object]
