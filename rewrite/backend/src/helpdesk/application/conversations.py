from helpdesk.application.identity import active
from helpdesk.domain.errors import AppError
from helpdesk.domain.models import MessageStatus, Principal, Row, Scope
from helpdesk.ports.repositories import UnitOfWork


class ConversationService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def create(self, principal: Principal, kb_ids: list[str]) -> Row:
        scope = Scope.from_ids(kb_ids)
        async with self.uow() as repos:
            current = await active(repos, principal)
            visible = {r["id"] for r in await repos.catalog.knowledge_bases(current)}
            if not set(scope.kb_ids).issubset(visible):
                raise AppError("invalid_scope", 400, "指定知识库范围无效")
            return await repos.conversations.create_session(current, list(scope.kb_ids))

    async def sessions(self, principal: Principal) -> list[Row]:
        async with self.uow() as repos:
            current = await active(repos, principal)
            rows = await repos.conversations.sessions(current)
            visible = {k["id"] for k in await repos.catalog.knowledge_bases(current)}
            for row in rows:
                ids = row["kb_ids"] or ([row["kb_id"]] if row["kb_id"] else [])
                restricted = not set(ids).issubset(visible)
                for message in await repos.conversations.messages(row["id"]):
                    if not await repos.conversations.provenance_visible(current, message["provenance"]):
                        restricted = True
                        break
                if restricted:
                    row.update(title="受限会话", kb_id=None, kb_ids=[])
            return rows

    async def history(self, principal: Principal, session_id: str) -> list[Row]:
        async with self.uow() as repos:
            current = await active(repos, principal)
            await repos.conversations.session(current, session_id)
            rows = await repos.conversations.messages(session_id)
            result = []
            for row in rows:
                visible = await repos.conversations.provenance_visible(current, row["provenance"])
                public = {k: row[k] for k in ("id", "session_id", "request_id", "question", "created_at")}
                public.update(
                    {
                        "status": row["status"] if visible else "restricted",
                        "answer": row["answer"] if visible else "",
                        "citations": row["citations"] if visible else [],
                        "trace": {},
                    }
                )
                result.append(public)
            return result

    async def admit(
        self, principal: Principal, session_id: str, request_id: str, question: str
    ) -> tuple[Row, bool]:
        if not request_id.strip() or len(request_id) > 200 or not question.strip() or len(question) > 8000:
            raise AppError("invalid_question", 400, "问题或请求编号无效")
        async with self.uow() as repos:
            return await repos.conversations.admit(
                session_id, request_id, question, await active(repos, principal)
            )

    async def finish(
        self, message_id: str, status: MessageStatus, answer: str, citations: list[Row], provenance: Row
    ) -> Row:
        if status == MessageStatus.GENERATING:
            raise AppError("invalid_transition", 409, "必须指定合法终态")
        if status in {MessageStatus.ERROR, MessageStatus.INTERRUPTED}:
            answer, citations = "", []
        async with self.uow() as repos:
            return await repos.conversations.finish(message_id, status.value, answer, citations, provenance)
