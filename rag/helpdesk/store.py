import json
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

    async def corpus(self, snapshot):
        versions = snapshot['version_ids']
        if not versions:
            return []
        rows = await self.rows('''SELECT c.id,c.version_id,c.ordinal,c.text,c.locator,c.content_hash,
            v.embedding_model,v.dimension,d.title,v.filename,v.source
            FROM chunks c JOIN document_versions v ON v.id=c.version_id JOIN documents d ON d.id=v.document_id
            WHERE c.version_id=ANY(%s) ORDER BY c.version_id,c.ordinal''', (versions,))
        if any(r['dimension'] != self.s.embedding_dimension or r['embedding_model'] != self.s.embedding_model for r in rows):
            raise ValueError('知识快照的 Embedding 模型/维度与当前配置不同，请重新导入')
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
