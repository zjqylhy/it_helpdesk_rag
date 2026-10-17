import hashlib
import json
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import Row


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


@dataclass(frozen=True)
class BoundScope:
    kb_ids: tuple[str, ...]
    knowledge_bases: tuple[Row, ...]
    version_ids: tuple[str, ...]
    snapshots: tuple[Row, ...]
    revision: int
    scope_hash: str


@dataclass(frozen=True)
class Chunk:
    id: str
    version_id: str
    document_id: str
    kb_id: str
    kb_name: str
    organization: str
    title: str
    text: str
    locator: Row
    content_hash: str

    def evidence(self, label: str) -> Row:
        return {
            "label": label,
            "chunk_id": self.id,
            "version_id": self.version_id,
            "document_id": self.document_id,
            "kb_id": self.kb_id,
            "kb_name": self.kb_name,
            "organization": self.organization,
            "title": self.title,
            "text": self.text,
            "locator": self.locator,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    output_applicable: bool = field(default=True, compare=False)
    input_source: str | None = field(default=None, compare=False)
    output_source: str | None = field(default=None, compare=False)
    raw_output_tokens: int | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Reply:
    value: Row
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True)
class TextDelta:
    text: str = ""
    finish_reason: str | None = None
    usage: Usage = field(default_factory=Usage)


class ScopeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=8000)
    behavior: Literal["search", "clarify", "refuse"]
    kb_ids: list[str]
    requires_all: bool
    message: str = ""
    option_ids: list[str] = Field(default_factory=list)


class Sufficiency(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    behavior: Literal["answer", "refuse", "clarify"]
    reason: str
    evidence_labels: list[str]


@dataclass(frozen=True)
class Lease:
    job_id: str
    version_id: str
    document_id: str
    kb_id: str
    owner: str
    attempt: int
    storage_key: str
    filename: str
    content_hash: str
    ingest_order: int


@dataclass(frozen=True)
class ParsedChunk:
    ordinal: int
    text: str
    locator: Row
    content_hash: str
