"""真实 Go/Python/数据库上的授权负例，替身模型捕获实际上下文。"""
import asyncio
import json
import os
import httpx
from helpdesk.config import Settings
from helpdesk.store import Store
from helpdesk.runtime import loop_factory


async def main():
    store=Store(Settings())
    legacy=await store.rows("SELECT visibility FROM documents WHERE id='legacy-doc'")
    if legacy:
        assert legacy[0]['visibility']=='all_users'
    async with httpx.AsyncClient(base_url=os.getenv('TEST_API_URL','http://127.0.0.1:8080/api/v1'),timeout=40,trust_env=False) as client, httpx.AsyncClient(base_url=Settings().deepseek_base_url,timeout=20,trust_env=False) as model:
        async def login(name,password):
            r=await client.post('/auth/login',json={'username':name,'password':password});r.raise_for_status()
            return {'Authorization':'Bearer '+r.json()['token']}
        admin=await login('admin','test-admin-password')
        user=await login('demo','test-user-password')
        assert (await client.get('/admin/users',headers=user)).status_code==403
        users=(await client.get('/admin/users',headers=admin)).json()
        uid=next(u['id'] for u in users if u['username']=='demo')
        r=await client.post('/knowledge-bases',headers=admin,json={'name':'权限契约库','organization':'权限测试组织'});r.raise_for_status();kb=r.json()['id']

        async def upload(text):
            r=await client.post('/documents',headers=admin,data={'kb_id':kb},files={'file':('access.txt',text.encode(),'text/plain')});r.raise_for_status();doc=r.json()
            for _ in range(100):
                rows=(await client.get('/documents',headers=admin,params={'kb_id':kb})).json()
                row=next(d for d in rows if d['id']==doc['document_id'])
                if row['job_status']=='failed':raise AssertionError(row)
                if row['active_version']==doc['version_id']:return doc
                await asyncio.sleep(.2)
            raise AssertionError('ingestion timeout')
        public=await upload('公开测试：打开客户端后，输入公开服务器。只用于权限测试。')
        secret=await upload('受限测试：SECRET-CONTEXT-437 应仅发送给授权成员。打开内部客户端。')
        assert (await client.get('/files/'+secret['version_id'],headers=user)).status_code==404
        assert (await client.get('/documents/'+secret['document_id']+'/versions',headers=user)).status_code==404
        assert kb not in {k['id'] for k in (await client.get('/knowledge-bases',headers=user)).json()}
        async def grant(doc,groups=None):
            r=await client.put('/admin/documents/'+doc['document_id']+'/access',headers=admin,json={'visibility':'all_users' if groups is None else 'groups','group_ids':groups or []});r.raise_for_status()
        await grant(public)
        listed=(await client.get('/documents',headers=user,params={'kb_id':kb})).json()
        assert {d['id'] for d in listed}=={public['document_id']}
        assert (await client.get(f"/documents/{public['document_id']}/versions/{secret['version_id']}/chunks",headers=user)).json()==[]
        await client.post('/admin/groups',headers=admin,json={'name':'受限资料成员'})
        gid=next(g['id'] for g in (await client.get('/admin/groups',headers=admin)).json() if g['name']=='受限资料成员')
        await grant(secret,[gid])

        async def session():
            r=await client.post('/sessions',headers=user,json={'kb_ids':[kb]});r.raise_for_status();return r.json()['id']
        sid=await session()
        async def ask(identity,question='如何连接客户端？'):
            r=await client.post('/chat',headers=user,json={'session_id':sid,'request_id':identity,'question':question});r.raise_for_status()
            return [(frame.splitlines()[0][7:],json.loads(frame.splitlines()[1][6:])) for frame in r.text.split('\n\n') if frame.startswith('event:')]
        await model.delete('/test/requests')
        events=await ask('without-group')
        assert events[-1][1]['status']=='completed',events
        assert {d['version_id'] for k,p in events if k=='evidence' for d in p['evidence']}=={public['version_id']}
        captured=(await model.get('/test/requests')).json()
        assert 'SECRET-CONTEXT-437' not in json.dumps(captured,ensure_ascii=False)
        r=await client.put('/admin/groups/'+gid+'/members',headers=admin,json={'user_ids':[uid]});r.raise_for_status()
        assert (await client.get('/files/'+secret['version_id'],headers=user)).status_code==200
        events=await ask('with-group')
        assert {d['version_id'] for k,p in events if k=='evidence' for d in p['evidence']}=={public['version_id'],secret['version_id']}

        # 在模型的慢速生成阶段撤销，不能继续输出或复用旧回答。
        async with client.stream('POST','/chat',headers=user,json={'session_id':sid,'request_id':'revoke-stream','question':'慢速连接客户端'}) as r:
            r.raise_for_status();frames=[]
            lines=r.aiter_lines()
            async for line in lines:
                frames.append(line)
                if line.startswith('data:') and '正在组织' in line:
                    revoked=await client.put('/admin/groups/'+gid+'/members',headers=admin,json={'user_ids':[]})
                    revoked.raise_for_status()
                    break
            tail='\n'.join([line async for line in lines])
            assert 'event: done' not in tail
        await asyncio.sleep(.2)
        history=(await client.get('/sessions/'+sid+'/messages',headers=user)).json()
        private=next(m for m in history if m['request_id']=='with-group')
        assert private['status']=='restricted' and private['citations']==[] and private['trace'] is None
        assert (await client.get('/files/'+secret['version_id'],headers=user)).status_code==404
        await model.delete('/test/requests')
        events=await ask('after-revoke','继续说明客户端')
        assert events[-1][1]['status']=='completed'
        assert 'SECRET-CONTEXT-437' not in json.dumps((await model.get('/test/requests')).json(),ensure_ascii=False)
        assert (await client.get('/sessions/'+sid+'/messages',headers=admin)).status_code==404

        # 未存在用户不能调用内部检索；关闭账号使已经发出的 Token 失效。
        settings=Settings()
        async with httpx.AsyncClient(base_url=os.getenv('TEST_RAG_URL','http://127.0.0.1:8000'),trust_env=False) as rag:
            r=await rag.post('/internal/chat',headers={'X-Internal-Token':settings.internal_token},json={'kb_ids':[kb],'question':'测试','request_id':'bad-principal','user_id':'missing-user','authorization_revision':0})
            assert r.status_code==403
        r=await client.patch('/admin/users/'+uid,headers=admin,json={'disabled':True});r.raise_for_status()
        assert (await client.get('/auth/me',headers=user)).status_code==401
        assert (await client.post('/auth/login',json={'username':'demo','password':'test-user-password'})).status_code==401
        await client.patch('/admin/users/'+uid,headers=admin,json={'disabled':False})
        assert (await client.get('/auth/me',headers=user)).status_code==401
        async with await store.connect() as conn:
            assert (await (await conn.execute("SELECT count(*) AS n FROM messages WHERE session_id=%s AND status='generating'",(sid,))).fetchone())['n']==0
        print('权限联测通过：新文档默认拒绝、同库文档隔离、上下文无泄露、组授予/撤销、SSE中止、历史/追问遮蔽、下载与版本、主体拒绝、账号停用和Token失效。')


if __name__=='__main__':asyncio.run(main(),loop_factory=loop_factory)
