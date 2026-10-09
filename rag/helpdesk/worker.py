import asyncio
import hashlib
import logging
from pathlib import Path
from psycopg.types.json import Jsonb
from .parsing import parse, chunk, PARSER_VERSION
from .providers import Models
from .store import Store, new_id

log = logging.getLogger(__name__)

async def claim(store, owner):
    async with await store.connect() as conn:
        cur = await conn.execute('''UPDATE ingestion_jobs SET status='processing',attempts=attempts+1,
            lease_owner=%s,lease_until=now()+interval '90 seconds',updated_at=now()
            WHERE id=(SELECT id FROM ingestion_jobs WHERE attempts<3 AND
            (status='queued' OR (status='processing' AND lease_until<now()))
            ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *''', (owner,))
        job = await cur.fetchone()
        if job:
            await conn.execute("UPDATE document_versions SET status='processing',error=NULL WHERE id=%s", (job['version_id'],))
        # 超过恢复次数的过期租约明确结束，防止任务永久悬挂。
        await conn.execute("UPDATE document_versions SET status='failed',error='租约恢复次数耗尽' WHERE id IN (SELECT version_id FROM ingestion_jobs WHERE status='processing' AND attempts>=3 AND lease_until<now())")
        await conn.execute("UPDATE ingestion_jobs SET status='failed',error='租约恢复次数耗尽' WHERE status='processing' AND attempts>=3 AND lease_until<now()")
        return job

async def heartbeat(store, job, owner):
    while True:
        await asyncio.sleep(20)
        async with await store.connect() as conn:
            await conn.execute("UPDATE ingestion_jobs SET lease_until=now()+interval '90 seconds',updated_at=now() WHERE id=%s AND lease_owner=%s AND status='processing'", (job['id'], owner))

async def process(store: Store, models: Models, job, owner):
    versions = await store.rows('SELECT * FROM document_versions WHERE id=%s', (job['version_id'],))
    version = versions[0]
    if hashlib.sha256(Path(version['path']).read_bytes()).hexdigest() != version['content_hash']:
        raise ValueError('原文件校验值变化')
    paragraphs = await asyncio.to_thread(parse, version['path'])
    chunks = chunk(paragraphs, store.s.chunk_size, store.s.chunk_overlap)
    vectors = []
    for offset in range(0, len(chunks), 20):
        vectors.extend(await models.embed([c['text'] for c in chunks[offset:offset+20]]))
        async with await store.connect() as conn:
            await conn.execute('UPDATE ingestion_jobs SET progress=%s WHERE id=%s AND lease_owner=%s', (min(95, round((offset+20)/len(chunks)*90)), job['id'], owner))
    async with await store.connect() as conn:
        cur = await conn.execute("SELECT * FROM ingestion_jobs WHERE id=%s AND lease_owner=%s AND status='processing' AND lease_until>now() FOR UPDATE", (job['id'], owner))
        if not await cur.fetchone():
            raise ValueError('任务租约丢失，取消发布')
        cur = await conn.execute('SELECT kb_id FROM documents WHERE id=%s', (version['document_id'],))
        kb = (await cur.fetchone())['kb_id']
        await conn.execute('SELECT id FROM knowledge_bases WHERE id=%s FOR UPDATE', (kb,))
        await conn.execute('DELETE FROM chunks WHERE version_id=%s', (version['id'],))
        import json
        for ordinal, (c, vector) in enumerate(zip(chunks, vectors)):
            await conn.execute('INSERT INTO chunks VALUES(%s,%s,%s,%s,%s,%s,%s::vector)',
                               (new_id(), version['id'], ordinal, c['text'], Jsonb(c['locator']), c['content_hash'], json.dumps(vector)))
        await conn.execute("UPDATE document_versions SET status='completed',error=NULL,parser_version=%s,embedding_model=%s,dimension=%s WHERE id=%s", (PARSER_VERSION, store.s.embedding_model, store.s.embedding_dimension, version['id']))
        # 旧任务晚完成不能覆盖已经发布的较新版本。
        await conn.execute('''UPDATE documents SET active_version=%s WHERE id=%s AND
            (active_version IS NULL OR (SELECT created_at FROM document_versions WHERE id=active_version)<=%s)''',
                           (version['id'], version['document_id'], version['created_at']))
        cur = await conn.execute('UPDATE knowledge_bases SET revision=revision+1 WHERE id=%s RETURNING revision', (kb,))
        revision = (await cur.fetchone())['revision']
        await conn.execute("INSERT INTO snapshots(id,kb_id,revision,version_ids) SELECT %s,%s,%s,COALESCE(jsonb_agg(active_version ORDER BY id),'[]') FROM documents WHERE kb_id=%s AND enabled AND active_version IS NOT NULL", (new_id(), kb, revision, kb))
        await conn.execute("UPDATE ingestion_jobs SET status='completed',progress=100,error=NULL,lease_until=NULL,lease_owner=NULL,updated_at=now() WHERE id=%s", (job['id'],))

async def worker(store, models):
    owner = new_id()
    while True:
        heartbeat_task = None
        job = None
        try:
            job = await claim(store, owner)
            if not job:
                await asyncio.sleep(2)
                continue
            heartbeat_task = asyncio.create_task(heartbeat(store, job, owner))
            await process(store, models, job, owner)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('导入失败 (%s)', type(exc).__name__)
            if job:
                # 错误只记录自定义分类，避免第三方异常携带凭据或原文。
                message = str(exc) if isinstance(exc, ValueError) or type(exc).__name__ == 'ProviderError' else '处理失败，请检查服务日志和配置'
                async with await store.connect() as conn:
                    cur = await conn.execute("UPDATE ingestion_jobs SET status='failed',error=%s,lease_until=NULL,lease_owner=NULL WHERE id=%s AND lease_owner=%s RETURNING version_id", (message, job['id'], owner))
                    if await cur.fetchone():
                        await conn.execute("UPDATE document_versions SET status='failed',error=%s WHERE id=%s", (message, job['version_id']))
            else:
                await asyncio.sleep(5)
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
