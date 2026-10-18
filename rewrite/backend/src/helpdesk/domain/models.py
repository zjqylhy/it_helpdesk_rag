from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import uuid4

from .errors import AppError

type Row = dict[str, Any]


def new_id() -> str:
    return uuid4().hex


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    role: str
    usage_kind: str

    def require_admin(self) -> None:
        if self.role != "admin":
            raise AppError("forbidden", 403, "需要管理员权限")


class MessageStatus(StrEnum):
    GENERATING = "generating"
    COMPLETED = "completed"
    REFUSED = "refused"
    CLARIFY = "clarify"
    ERROR = "error"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class Scope:
    kb_ids: tuple[str, ...]
    mode: str

    @classmethod
    def from_ids(cls, kb_ids: list[str]) -> "Scope":
        ids = tuple(dict.fromkeys(kb_ids))
        if len(ids) > 32 or any(not x.strip() for x in ids):
            raise AppError("invalid_scope", 400, "指定知识库范围无效")
        return cls(ids, "selected" if ids else "all")
