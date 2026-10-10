"""隔离测试库中验证缺省范围、多库会话、澄清承接和来源隔离。"""
import asyncio
import json
import os

import httpx

from helpdesk.config import Settings
from helpdesk.runtime import loop_factory
from helpdesk.store import Store


async def main():
    store = Store(Settings())
    async with httpx.AsyncClient(base_url=os.getenv('TEST_API_URL', 'http://127.0.0.1:8080/api/v1'), timeout=90, trust_env=False) as client:
        async def login(name, password):
            response = await client.post('/auth/login', json={'username': name, 'password': password})
            response.raise_for_status()
            return {'Authorization': 'Bearer ' + response.json()['token']}
        admin = await login('admin', 'test-admin-password')
        user = await login('demo', 'test-user-password')

        async def create(name, organization, enabled=True):
            response = await client.post('/knowledge-bases', headers=admin,
                json={'name': name, 'organization': organization, 'default_search': enabled})
            response.raise_for_status()
            kb = response.json()['id']
            response = await client.post('/documents', headers=admin, data={'kb_id': kb},
                files={'file': (name + '.txt', (organization + 'VPN：打开客户端并输入本校服务器地址。').encode(), 'text/plain')})
            response.raise_for_status()
            imported = response.json()
            grant=await client.put('/admin/documents/'+imported['document_id']+'/access',headers=admin,json={'visibility':'all_users','group_ids':[]})
            grant.raise_for_status()
            for _ in range(100):
                response = await client.get('/documents', headers=admin, params={'kb_id': kb})
                response.raise_for_status()
                row = response.json()[0]
                if row['job_status'] == 'failed':
                    raise AssertionError('Fixture ingestion failed')
                if row['active_version'] == imported['version_id']:
                    return kb, imported
                await asyncio.sleep(.2)
            raise AssertionError('Fixture ingestion timed out')

        alpha, alpha_doc = await create('甲大学 VPN', '甲大学')
        beta, beta_doc = await create('乙大学 VPN', '乙大学')
        excluded, _ = await create('部署验证 VPN', '验证组织', False)
        bound = await store.scope_snapshot([])
        assert excluded not in {m['kb_id'] for m in bound['members']}
        assert excluded in {m['kb_id'] for m in (await store.scope_snapshot([excluded]))['members']}
        assert (await client.patch(f'/knowledge-bases/{alpha}/default-search', headers=user, json={'enabled': False})).status_code == 403
        assert (await client.patch('/knowledge-bases/not-found/default-search', headers=admin, json={'enabled': False})).status_code == 404
        assert (await client.post('/sessions', headers=user, json={'kb_ids': ['not-found']})).status_code == 400
        assert (await client.post('/sessions', headers=user, json={'kb_id': alpha, 'kb_ids': [beta]})).status_code == 400

        async def session(scope):
            response = await client.post('/sessions', headers=user, json=scope)
            response.raise_for_status()
            return response.json()['id']

        async def ask(sid, question, identity):
            response = await client.post('/chat', headers=user,
                json={'session_id': sid, 'question': question, 'request_id': identity})
            response.raise_for_status()
            result = []
            for frame in response.text.split('\n\n'):
                lines = frame.splitlines()
                if len(lines) >= 2:
                    result.append((lines[0][7:], json.loads(lines[1][6:])))
            assert result[-1][0] == 'done', result
            return result

        global_session = await session({})
        events = await ask(global_session, 'VPN怎么登录', 'clarify')
        assert events[-1][1]['status'] == 'clarify'
        question = next(d['text'] for kind, d in events if kind == 'refusal')
        assert '甲大学' in question and '乙大学' in question and '验证组织' not in question
        events = await ask(global_session, '甲大学', 'school-reply')
        assert events[-1][1]['status'] == 'completed'
        assert 'VPN怎么登录' in events[-1][1]['trace']['query']
        citations = next(d['citations'] for kind, d in events if kind == 'citations')
        assert {c['kb_id'] for c in citations} == {alpha}
        response = await client.get(f'/sessions/{global_session}/messages', headers=user)
        response.raise_for_status()
        assert [m['status'] for m in response.json()] == ['clarify', 'completed']
        assert (await client.get(f'/sessions/{global_session}/messages', headers=admin)).status_code == 404
        events = await ask(global_session, '比较甲大学和乙大学VPN登录方法', 'comparison')
        assert events[-1][1]['status'] == 'completed'
        assert {c['kb_id'] for kind, d in events if kind == 'citations' for c in d['citations']} == {alpha, beta}

        selected = await session({'kb_ids': [beta, alpha, beta]})
        response = await client.get('/sessions', headers=user)
        response.raise_for_status()
        assert next(s['kb_ids'] for s in response.json() if s['id'] == selected) == sorted([alpha, beta])
        events = await ask(selected, '比较甲大学和乙大学VPN登录方法', 'selected-comparison')
        assert {m['kb_id'] for m in events[-1][1]['trace']['available_snapshots']} == {alpha, beta}
        single = await session({'kb_id': alpha})
        assert (await ask(single, 'VPN怎么登录', 'single'))[-1][1]['status'] == 'completed'

        # 模拟迁移前的单库会话，旧历史和范围必须仍可读取。
        async with await store.connect() as conn:
            await conn.execute("UPDATE sessions SET kb_ids='[]' WHERE id=%s", (single,))
        assert (await ask(single, 'VPN怎么登录', 'legacy'))[-1][1]['status'] == 'completed'
        frozen = await store.scope_snapshot([alpha, beta])
        response = await client.patch('/documents/' + beta_doc['document_id'] + '/enabled', headers=admin, json={'enabled': False})
        response.raise_for_status()
        current = await store.scope_snapshot([alpha, beta])
        assert frozen['id'] != current['id']
        assert beta_doc['version_id'] in frozen['version_ids'] and beta_doc['version_id'] not in current['version_ids']
        assert any(c['version_id'] == beta_doc['version_id'] for c in await store.corpus(frozen))
        assert all(c['version_id'] != beta_doc['version_id'] for c in await store.corpus(current))
        print('跨库契约通过：默认排除验证库、非法范围、权限、澄清追问、多库比较、来源标注、旧会话兼容、成员快照与停用。')


if __name__ == '__main__':
    asyncio.run(main(), loop_factory=loop_factory)
