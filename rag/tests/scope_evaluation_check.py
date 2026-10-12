"""真实数据库、Go授权上传、本机模型替身上的跨库评测 CLI 契约。"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx

from helpdesk.config import Settings
from helpdesk.store import Store
from helpdesk.access import AuthorizedStore
from helpdesk.runtime import loop_factory
from helpdesk.evaluation.scope_store import export_scope, FrozenScopeStore
from helpdesk.evaluation.scope_dataset import ScopeCase
from helpdesk.evaluation.dataset import Anchor, save


async def prepare(root):
    async with httpx.AsyncClient(base_url=os.environ['TEST_API_URL'], timeout=30, trust_env=False) as client:
        r = await client.post('/auth/login', json={'username': 'admin', 'password': 'test-admin-password'}); r.raise_for_status()
        admin = {'Authorization': 'Bearer ' + r.json()['token']}
        ids = []
        for name in ['评测甲大学', '评测乙大学']:
            r = await client.post('/knowledge-bases', headers=admin, json={'name': name + 'VPN', 'organization': name}); r.raise_for_status()
            kb = r.json()['id']; ids.append(kb)
            r = await client.post('/documents', headers=admin, data={'kb_id': kb}, files={'file': ('eval.txt', (name + '登录：打开客户端并输入组织服务器地址。').encode(), 'text/plain')}); r.raise_for_status()
            doc = r.json()
            r = await client.put('/admin/documents/' + doc['document_id'] + '/access', headers=admin, json={'visibility': 'all_users', 'group_ids': []}); r.raise_for_status()
            for _ in range(100):
                entries = (await client.get('/documents', headers=admin, params={'kb_id': kb})).json()
                if entries[0]['active_version'] == doc['version_id']:
                    break
                await asyncio.sleep(.1)
            else:
                raise AssertionError('导入未完成')
    settings = Settings(); store = Store(settings)
    uid = (await store.rows("SELECT id FROM users WHERE username='demo'"))[0]['id']
    revision = (await store.rows('SELECT revision FROM authorization_state WHERE id=1'))[0]['revision']
    bundle = await export_scope(AuthorizedStore(settings, uid, revision))
    await FrozenScopeStore(settings, bundle).verify()
    paragraphs = [p for p in bundle['corpus']['paragraphs'] if p['kb_id'] in ids]
    case = ScopeCase(id='contract-scope', split='dev', theme='synthetic-comparison', scene='comparison',
        status='active', review_record={'reviewer': '工程契约夹具', 'approved_at': '2026-10-12', 'confirmation': '仅工程测试，不是真实知识效果裁决'},
        scope_version=bundle['scope_version'], scope_mode='selected', kb_ids=ids,
        expected_route_kb_ids=ids, expected_required_kb_ids=ids,
        question='比较评测甲大学和评测乙大学VPN登录方法', expected_behavior='answer', required_facts=['分别使用对应组织的服务器'],
        evidence_groups=[[Anchor(source_version=p['version_id'], locator=p['locator'], quote=p['text'])] for p in paragraphs])
    save(root / 'cases.jsonl', [case.model_dump()], exclusive=True)
    (root / 'corpus.json').write_text(json.dumps(bundle, ensure_ascii=False), encoding='utf-8')
    return ids, bundle


def main():
    root = Path(os.environ['EFFECT_CHECK_ROOT']) / 'scope-eval'; root.mkdir(parents=True)
    ids, bundle = asyncio.run(prepare(root), loop_factory=loop_factory)
    def cli(*args, success=True):
        r = subprocess.run([sys.executable, '-m', 'helpdesk.evaluation.cli', *map(str, args)], capture_output=True, text=True, encoding='utf-8')
        assert (r.returncode == 0) == success, r.stdout + r.stderr
        return r
    cli('scope-validate', root / 'cases.jsonl', '--corpus', root / 'corpus.json')
    cli('scope-publish', root / 'cases.jsonl', '--corpus', root / 'corpus.json', '--directory', root / 'releases', '--version', 'v1')
    for experiment in ['baseline', 'split-v1']:
        args = ['scope-run', root / 'cases.jsonl', '--corpus', root / 'corpus.json', '--out', root / experiment,
                '--experiment', experiment, '--max-calls', '20', '--judge']
        cli(*args); cli(*args, '--resume')
        rows = [json.loads(s) for s in (root / experiment / 'cases.jsonl').read_text(encoding='utf-8').splitlines()]
        assert len(rows) == 1 and rows[0]['status'] == 'completed'
        assert {c['kb_id'] for c in rows[0]['citations']} == set(ids)
        if experiment == 'split-v1':
            assert rows[0]['trace']['decomposition']['status'] == 'split'
        summary = json.loads((root / experiment / 'scope-summary.json').read_text(encoding='utf-8'))
        assert summary['pending'] == 0 and summary['behaviors']['answer']['official_correctness'] is None
    cli('scope-packet', root / 'cases.jsonl', '--corpus', root / 'corpus.json', '--run', root / 'baseline', '--out', root / 'review')
    pair_args = ['scope-experiment', root / 'cases.jsonl', '--corpus', root / 'corpus.json', '--out', root / 'paired', '--seed', '12', '--max-calls', '40']
    cli(*pair_args); cli(*pair_args, '--resume')
    cli('scope-pair-packet', root / 'cases.jsonl', '--corpus', root / 'corpus.json', '--left', root / 'paired/baseline', '--right', root / 'paired/split-v1', '--seed', '12', '--out', root / 'blind')
    packet = json.loads((root / 'blind/review.json').read_text(encoding='utf-8'))
    assert 'split-v1' not in json.dumps(packet['results'])
    async def drift():
        store = Store(Settings())
        async with await store.connect() as conn:
            await conn.execute('UPDATE knowledge_bases SET default_search=false WHERE id=ANY(%s)', (ids,))
        try:
            await FrozenScopeStore(Settings(), bundle).verify()
        except ValueError:
            pass
        else:
            raise AssertionError('默认配置漂移未拒绝')
    asyncio.run(drift(), loop_factory=loop_factory)
    print('跨库评测契约通过：真实授权导出/固定重放、B0/B1、拆解、比较引用、冻结、无调用续跑及配置漂移拒绝。')


if __name__ == '__main__':
    main()
