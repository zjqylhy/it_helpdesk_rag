from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from helpdesk.domain.contracts import PublicEvent
from helpdesk.domain.models import Principal, Row
from helpdesk.domain.rag import BoundScope


@dataclass(frozen=True)
class ChatRunInput:
    request_id: str
    message_id: str
    principal: Principal
    authorization_revision: int
    requested_scope: tuple[str, ...]
    question: str
    history: tuple[Row, ...]
    server_strategy_id: str
    deadline: float
    schema_version: str = "chat-run-v1"


class ChatRunner(Protocol):
    def events(self) -> AsyncIterator[PublicEvent]: ...


class ChatPersistence(Protocol):
    async def prepare(
        self,
        principal: Principal,
        session_id: str,
        request_id: str,
        question: str,
        scope: BoundScope,
        strategy: str,
    ) -> tuple[Row, list[Row], Row]: ...
    async def save_provenance(self, message_id: str, provenance: Row) -> None: ...
    async def finish(
        self, message_id: str, status: str, answer: str, citations: list[Row], trace: Row
    ) -> None: ...
    async def abort(self, message_id: str, status: str, reason: str) -> str: ...
    async def progress(self, message_id: str, answer: str) -> None: ...
