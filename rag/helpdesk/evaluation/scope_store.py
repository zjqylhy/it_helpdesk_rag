"""通过在线授权边界重放固定成员，既不采用最新快照，也不放宽主体权限。"""
from ..access import AuthorizedStore, AccessDenied
from ..store import Store, combine_snapshots
from .dataset import sha
from .scope_dataset import seal_bundle, validate_bundle


async def principal_record(store):
    rows = await store.rows('SELECT role FROM users WHERE id=%s AND NOT disabled', (store.user_id,))
    if not rows:
        raise AccessDenied('评测主体不可用')
    groups = await store.rows('SELECT group_id FROM user_group_members WHERE user_id=%s ORDER BY group_id', (store.user_id,))
    return {'user_id': store.user_id, 'revision': store.revision, 'role': rows[0]['role'], 'group_ids': [g['group_id'] for g in groups]}


async def export_scope(store):
    await store.bind()
    if not store.kbs:
        raise ValueError('评测主体没有可读知识库')
    # 所有可见库一同绑定，保留 default_search 供逐题默认/指定范围选择。
    raw = await Store.scope_snapshot(store, sorted(store.kbs))
    metadata = await store.rows('SELECT id,default_search FROM knowledge_bases WHERE id=ANY(%s)', (sorted(store.kbs),))
    defaults = {m['id']: m['default_search'] for m in metadata}
    members = [{**{k: m[k] for k in ('kb_id', 'name', 'organization')}, 'snapshot_id': m['id'],
                'snapshot_version_hash': sha(m['version_ids']), 'default_search': defaults[m['kb_id']],
                'version_ids': [v for v in (m['version_ids'] or []) if v in store.versions]}
               for m in raw['members'] if m['id'] and any(v in store.versions for v in (m['version_ids'] or []))]
    bound = combine_snapshots([{**m, 'id': m['snapshot_id']} for m in members])
    chunks = await store.corpus(bound)
    principal = await principal_record(store)
    await store.check()
    return seal_bundle(members, principal, chunks)


class FrozenScopeStore(AuthorizedStore):
    def __init__(self, settings, bundle):
        validate_bundle(bundle)
        principal = bundle['principal']
        super().__init__(settings, principal['user_id'], principal['revision'])
        self.bundle = bundle

    async def verify(self):
        await self.bind()
        if await principal_record(self) != self.bundle['principal']:
            raise ValueError('冻结主体/组成员与当前授权状态不一致')
        ids = [m['kb_id'] for m in self.bundle['members']]
        if not set(ids) <= self.kbs:
            raise AccessDenied('冻结成员已不可访问')
        rows = await self.rows('SELECT id,name,organization,default_search FROM knowledge_bases WHERE id=ANY(%s)', (ids,))
        current = {r['id']: r for r in rows}
        for m in self.bundle['members']:
            if m['kb_id'] not in current or any(current[m['kb_id']][k] != m[k] for k in ('name', 'organization', 'default_search')):
                raise ValueError('知识库组织或默认范围配置漂移')
            raw = await Store.snapshot(self, m['kb_id'], m['snapshot_id'])
            if sha(raw['version_ids']) != m['snapshot_version_hash']:
                raise ValueError('冻结快照成员漂移')
            if [v for v in raw['version_ids'] if v in self.versions] != m['version_ids']:
                raise ValueError('冻结文档授权集合漂移')
        bound = self.bound(self.bundle['members'])
        actual = await self.corpus(bound)
        if sha(actual) != sha(self.bundle['corpus']['chunks']):
            raise ValueError('数据库原文与冻结语料不一致')
        await self.check()

    def bound(self, members):
        return combine_snapshots([self.restrict({**m, 'id': m['snapshot_id']}) for m in members])

    async def scope_snapshot(self, kb_ids):
        await self.check()
        available = {m['kb_id'] for m in self.bundle['members']}
        if kb_ids and not set(kb_ids) <= available:
            raise AccessDenied('资料不存在或不可访问')
        selected = [m for m in self.bundle['members'] if m['kb_id'] in kb_ids or (not kb_ids and m['default_search'])]
        return self.bound(selected)

    async def corpus(self, snapshot):
        versions = set(snapshot['version_ids'])
        frozen_versions = {v for m in self.bundle['members'] for v in m['version_ids']}
        if not versions <= frozen_versions:
            raise AccessDenied('证据超出冻结范围')
        actual = await super().corpus(snapshot)
        expected = [c for c in self.bundle['corpus']['chunks'] if c['version_id'] in versions]
        if 'members' not in snapshot:
            expected = [{k: v for k, v in c.items() if k not in ('kb_id', 'knowledge_base_name', 'organization')} for c in expected]
        if sha(actual) != sha(expected):
            raise ValueError('检索时原文或组织元数据发生漂移')
        return actual
