"""在线检索在进入模型前收敛到主体可见的文档版本，不改变旧离线快照。"""
import hashlib
import json
from .store import Store, combine_snapshots


class AccessDenied(ValueError):
    pass


class AuthorizedStore(Store):
    def __init__(self, settings, user_id, revision):
        super().__init__(settings)
        self.user_id, self.revision = user_id, revision
        self.versions, self.kbs = set(), set()

    async def bind(self):
        await self.check()
        rows = await self.rows('''SELECT d.kb_id,v.id FROM documents d
            JOIN users u ON u.id=%s LEFT JOIN document_versions v ON v.document_id=d.id
            WHERE NOT u.disabled AND (u.role='admin' OR d.visibility='all_users' OR EXISTS(
            SELECT 1 FROM document_group_grants g JOIN user_group_members m ON m.group_id=g.group_id
            WHERE g.document_id=d.id AND m.user_id=u.id))''', (self.user_id,))
        self.versions = {r['id'] for r in rows if r['id']}
        self.kbs = {r['kb_id'] for r in rows}

    async def check(self):
        rows = await self.rows('''SELECT s.revision FROM authorization_state s JOIN users u ON u.id=%s
            WHERE s.id=1 AND NOT u.disabled''', (self.user_id,))
        if not rows or rows[0]['revision'] != self.revision:
            raise AccessDenied('资料权限已变更，请刷新后重试')

    def restrict(self, snapshot):
        versions = [v for v in snapshot['version_ids'] if v in self.versions]
        digest = hashlib.sha256(json.dumps([versions, self.revision]).encode()).hexdigest()
        return {**snapshot, 'version_ids': versions, 'id': snapshot['id'] + '-acl-' + digest}

    async def scope_snapshot(self, kb_ids):
        await self.check()
        if kb_ids and not set(kb_ids) <= self.kbs:
            raise AccessDenied('资料不存在或不可访问')
        snapshot = await super().scope_snapshot(kb_ids)
        members = [self.restrict(m) for m in snapshot['members'] if m['kb_id'] in self.kbs and m['id']]
        return combine_snapshots(members)

    async def snapshot(self, kb_id, snapshot_id=None):
        await self.check()
        if kb_id not in self.kbs:
            raise AccessDenied('资料不存在或不可访问')
        return self.restrict(await super().snapshot(kb_id, snapshot_id))

    async def corpus(self, snapshot):
        await self.check()
        return await super().corpus(self.restrict(snapshot))

    async def vector_search(self, snapshot, vector, limit=20):
        await self.check()
        return await super().vector_search(self.restrict(snapshot), vector, limit)
