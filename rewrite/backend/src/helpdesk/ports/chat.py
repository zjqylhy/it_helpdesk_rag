from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from helpdesk.domain.contracts import PublicEvent
from helpdesk.domain.models import Principal, Row


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
    def events(self, run: ChatRunInput) -> AsyncIterator[PublicEvent]: ...
