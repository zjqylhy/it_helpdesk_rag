import json
import hashlib
import uuid
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from .config import Settings

def new_id():
    return uuid.uuid4().hex

class Store:
    def __init__(self, settings: Settings):
        self.s = settings

    async def connect(self):
        return await psycopg.AsyncConnection.connect(self.s.dsn(), row_factory=dict_row, prepare_threshold=None)

    async def rows(self, sql, args=()):
        async with await self.connect() as conn:
            cur = await conn.execute(sql, args)
            return await cur.fetchall()

    async def snapshot(self, kb_id, snapshot_id=None):
        rows = await self.rows('SELECT * FROM snapshots WHERE kb_id=%s ' +
                               ('AND id=%s' if snapshot_id else 'ORDER BY revision DESC LIMIT 1'),
                               (kb_id, snapshot_id) if snapshot_id else (kb_id,))
        if not rows:
            raise ValueError('知识库尚无发布快照，请先上传并完成处理')
        return rows[0]

    async def scope_snapshot(self, kb_ids):
        # 一条 SQL 的读取视图绑定所有成员，防止不同库发布时拼接新旧状态。
        members = await self.rows('''SELECT k.id AS kb_id,k.name,k.organization,
            s.id,s.version_ids FROM knowledge_bases k
            LEFT JOIN LATERAL (SELECT id,version_ids FROM snapshots
                WHERE kb_id=k.id ORDER BY revision DESC LIMIT 1) s ON true
            WHERE k.id=ANY(%s) OR (%s AND k.default_search) ORDER BY k.id''',
            (kb_ids, not kb_ids))
        if kb_ids and {m['kb_id'] for m in members} != set(kb_ids):
            raise ValueError('指定知识库不存在')
        return combine_snapshots(members)

    async def corpus(self, snapshot):
        versions = snapshot['version_ids']
        if not versions:
            return []
        rows = await self.rows('''SELECT c.id,c.version_id,c.ordinal,c.text,c.locator,c.content_hash,
            v.embedding_model,v.dimension,d.title,v.filename,v.source,
            k.id AS kb_id,k.name AS knowledge_base_name,k.organization
            FROM chunks c JOIN document_versions v ON v.id=c.version_id JOIN documents d ON d.id=v.document_id
            JOIN knowledge_bases k ON k.id=d.kb_id
            WHERE c.version_id=ANY(%s) ORDER BY c.version_id,c.ordinal''', (versions,))
        if any(r['dimension'] != self.s.embedding_dimension or r['embedding_model'] != self.s.embedding_model for r in rows):
            raise ValueError('知识快照的 Embedding 模型/维度与当前配置不同，请重新导入')
        if 'members' not in snapshot:
            # 单库离线快照格式保持不变，新增展示元数据不改变已冻结语料的哈希。
            rows = [{k: v for k, v in r.items() if k not in ('kb_id', 'knowledge_base_name', 'organization')} for r in rows]
        return rows

    async def vector_search(self, snapshot, vector, limit=20):
        vector_literal = json.dumps(vector)
        return await self.rows('''SELECT id FROM chunks WHERE version_id=ANY(%s)
            ORDER BY embedding <=> %s::vector, id LIMIT %s''', (snapshot['version_ids'], vector_literal, limit))

    async def export_snapshot(self, kb_id, snapshot_id=None):
        snapshot = await self.snapshot(kb_id, snapshot_id)
        chunks = await self.corpus(snapshot)
        # 原文段落由带区间的切片还原；重叠部分必须一致。
        paragraphs = {}
        for c in chunks:
            loc = c['locator']
            key = (c['version_id'], loc.get('page'), loc.get('paragraph'), loc.get('section'))
            p = paragraphs.setdefault(key, {'version_id': c['version_id'], 'locator': {k: v for k, v in loc.items() if k not in ('start','end')}, 'text': ''})
            start = loc['start']
            overlap = max(0, len(p['text']) - start)
            if p['text'][start:start + overlap] != c['text'][:overlap]:
                raise ValueError('原文区间不一致')
            p['text'] += c['text'][overlap:]
        return {'knowledge_base': kb_id, 'corpus_version': snapshot['id'], 'chunks': chunks, 'paragraphs': list(paragraphs.values())}


def combine_snapshots(members):
    members = sorted(members, key=lambda m: m['kb_id'])
    identity = [[m['kb_id'], m['id'], m['name'], m['organization']] for m in members]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return {'id': 'scope-' + digest, 'members': members,
            'version_ids': [v for m in members for v in (m['version_ids'] or [])]}
