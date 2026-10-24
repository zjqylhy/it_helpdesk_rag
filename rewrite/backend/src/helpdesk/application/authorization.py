import asyncio
from dataclasses import dataclass, field, replace

from helpdesk.application.barrier import AuthorizationBarrier
from helpdesk.domain.errors import AppError
from helpdesk.domain.models import Principal, Row
from helpdesk.domain.rag import BoundScope, Chunk
from helpdesk.ports.chat import ChatPersistence
from helpdesk.ports.corpus import AuthorizedCorpus
from helpdesk.ports.repositories import UnitOfWork

DEPENDENCY_SCHEMA = "authorization-dependencies-v1"


@dataclass
class RunDependencies:
    """只记录真实使用的数据；原始候选范围仍由 BoundScope 保留。"""

    version_ids: set[str] = field(default_factory=set)
    kb_ids: set[str] = field(default_factory=set)
    history_ids: set[str] = field(default_factory=set)

    def include(self, provenance: Row) -> None:
        self.version_ids.update(provenance.get("version_ids", []))
        self.kb_ids.update(provenance.get("kb_ids", []))
        self.history_ids.update(provenance.get("history_ids", []))

    def provenance(self, scope: BoundScope) -> Row:
        return {
            "schema": DEPENDENCY_SCHEMA,
            "version_ids": sorted(self.version_ids),
            "kb_ids": sorted(self.kb_ids),
            "history_ids": sorted(self.history_ids),
            "authorization_revision": scope.revision,
            "scope_hash": scope.scope_hash,
            "snapshot_members": list(scope.snapshots),
        }


class RunAuthorization:
    def __init__(
        self,
        uow: UnitOfWork,
        barrier: AuthorizationBarrier,
        corpus: AuthorizedCorpus,
        store: ChatPersistence,
        principal: Principal,
        token_hash: str,
        message_id: str,
        scope: BoundScope,
        dependencies: RunDependencies,
    ) -> None:
        self.uow, self.barrier, self.corpus, self.store = uow, barrier, corpus, store
        self.principal, self.token_hash, self.message_id = principal, token_hash, message_id
        self.scope, self.dependencies = scope, dependencies
        self.failure: str | None = None
        self.read_lock = asyncio.Lock()

    async def check(self) -> None:
        """调用者持有读或写屏障；复核失败不能放行缓存中的资料。"""
        if self.failure:
            raise AppError(
                self.failure,
                409 if self.failure == "authorization_changed" else 503,
                "访问权限已失效" if self.failure == "authorization_changed" else "权限检查失败",
            )
        for attempt in range(2):
            try:
                async with asyncio.timeout(2):
                    async with self.uow() as repos:
                        current = await repos.identity.principal(self.token_hash)
                    if current is None or current.user_id != self.principal.user_id:
                        raise AppError("authorization_changed", 409, "登录或权限已失效")
                    await self.corpus.check(
                        current,
                        replace(
                            self.scope,
                            version_ids=tuple(self.dependencies.version_ids),
                            kb_ids=tuple(self.dependencies.kb_ids),
                        ),
                    )
                return
            except AppError as error:
                if error.code == "authorization_changed":
                    self.failure = error.code
                raise
            except Exception as error:
                if attempt:
                    self.failure = "authorization_check_failed"
                    raise AppError(self.failure, 503, "权限检查失败，请稍后重试") from error
                await asyncio.sleep(0.05)

    async def guarded_check(self) -> None:
        async with self.barrier.read():
            await self.check()

    async def save(self) -> None:
        await self.store.save_provenance(self.message_id, self.dependencies.provenance(self.scope))


class TrackedCorpus:
    """读取和登记共享读屏障，防止撤权遗漏刚进入处理的数据。"""

    def __init__(self, authorization: RunAuthorization) -> None:
        self.authorization = authorization

    async def bind(self, principal: Principal, kb_ids: tuple[str, ...], revision: int) -> BoundScope:
        return await self.authorization.corpus.bind(principal, kb_ids, revision)

    async def check(self, principal: Principal, scope: BoundScope) -> None:
        await self.authorization.guarded_check()

    async def metadata(self, principal: Principal, scope: BoundScope) -> BoundScope:
        auth = self.authorization
        async with auth.barrier.read(), auth.read_lock:
            await auth.check()
            visible = await auth.corpus.metadata(principal, scope)
            if visible.version_ids:
                auth.dependencies.kb_ids.update(kb["id"] for kb in visible.knowledge_bases)
                await auth.save()
            return visible

    async def chunks(self, principal: Principal, scope: BoundScope, kb_id: str) -> list[Chunk]:
        auth = self.authorization
        async with auth.barrier.read(), auth.read_lock:
            await auth.check()
            chunks = await auth.corpus.chunks(principal, scope, kb_id)
            auth.dependencies.version_ids.update(chunk.version_id for chunk in chunks)
            auth.dependencies.kb_ids.update(chunk.kb_id for chunk in chunks)
            await auth.save()
            return chunks

    async def vector(
        self, principal: Principal, scope: BoundScope, kb_id: str, embedding: list[float]
    ) -> list[Chunk]:
        auth = self.authorization
        async with auth.barrier.read(), auth.read_lock:
            await auth.check()
            if kb_id not in scope.kb_ids:
                raise AppError("invalid_scope", 400, "检索范围无效")
            candidates = await auth.corpus.metadata(principal, replace(scope, kb_ids=(kb_id,)))
            chunks = await auth.corpus.vector(principal, scope, kb_id, embedding)
            # 向量排序比较了整个候选集合，未进入 Top-20 的来源也会影响结果。
            auth.dependencies.version_ids.update(candidates.version_ids)
            auth.dependencies.kb_ids.update(chunk.kb_id for chunk in chunks)
            await auth.save()
            return chunks
