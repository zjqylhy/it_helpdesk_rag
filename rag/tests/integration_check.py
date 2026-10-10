"""对运行中的 Go/Python/SQL 服务进行工程集成验证，不调用付费模型。"""
import asyncio
import json
import os
import httpx
from pathlib import Path

async def main():
    async with httpx.AsyncClient(base_url=os.getenv('TEST_API_URL', 'http://127.0.0.1:8080/api/v1'),timeout=30,trust_env=False) as client:
        login=await client.post('/auth/login',json={'username':'admin','password':'test-admin-password'});login.raise_for_status()
        admin={'Authorization':'Bearer '+login.json()['token']}
        login=await client.post('/auth/login',json={'username':'demo','password':'test-user-password'});login.raise_for_status()
        user={'Authorization':'Bearer '+login.json()['token']}
        kb=(await client.post('/knowledge-bases',headers=admin,json={'name':'工程测试资料','organization':'虚构测试组织'})).json()['id']
        assert (await client.post('/knowledge-bases',headers=user,json={'name':'非法','organization':'测试'})).status_code==403
        async def upload(data,doc=None):
            r=await client.post('/documents'+('/'+doc+'/versions' if doc else ''),headers=admin,
                                data={'kb_id':kb},files={'file':('contract.txt',data,'text/plain')});r.raise_for_status()
            imported=r.json()
            if doc is None:
                grant=await client.put('/admin/documents/'+imported['document_id']+'/access',headers=admin,json={'visibility':'all_users','group_ids':[]})
                grant.raise_for_status()
            return imported
        async def wait_job(version):
            for _ in range(80):
                docs=(await client.get('/documents',params={'kb_id':kb},headers=admin)).json()
                target=next((d for d in docs if d['latest_version']==version),None)
                if target and target['job_status'] in ('completed','failed'):return target
                await asyncio.sleep(.2)
            raise AssertionError('导入任务超时')
        v1=await upload('工程测试素材，不代表真实制度。请打开客户端，再输入组织提供的服务器地址。'.encode())
        d1=await wait_job(v1['version_id']);assert d1['active_version']==v1['version_id'],d1
        session=(await client.post('/sessions',headers=user,json={'kb_id':kb})).json()['id']
        assert (await client.get(f'/sessions/{session}/messages',headers=admin)).status_code==404
        async def ask(question,request_id):
            response=await client.post('/chat',headers=user,json={'session_id':session,'question':question,'request_id':request_id});response.raise_for_status()
            events=[]
            for frame in response.text.split('\n\n'):
                rows=frame.splitlines()
                if len(rows)>=2:events.append((rows[0][7:],json.loads(rows[1][6:])))
            return events
        events=await ask('如何连接？','first');assert events[-1][0]=='done',events
        citations=next(d['citations'] for k,d in events if k=='citations');assert citations[0]['version_id']==v1['version_id']
        assert (await client.post('/chat',headers=user,json={'session_id':session,'question':'同一个问题','request_id':'first'})).status_code==409
        assert (await client.post('/documents',headers=admin,data={'kb_id':kb},files={'file':('contract.txt','工程测试素材，不代表真实制度。请打开客户端，再输入组织提供的服务器地址。'.encode(),'text/plain')})).status_code==409
        failed=await upload(b'bad',v1['document_id']);df=await wait_job(failed['version_id']);assert df['job_status']=='failed' and df['active_version']==v1['version_id']
        v2=await upload('工程测试第二版，请使用新的客户端，再填写组织提供的服务器地址。这里只测试版本切换。'.encode(),v1['document_id'])
        d2=await wait_job(v2['version_id']);assert d2['active_version']==v2['version_id']
        events=await ask('新版本如何连接？','second');assert next(d for k,d in events if k=='citations')['citations'][0]['version_id']==v2['version_id']
        assert (await client.get('/files/'+v1['version_id'],headers=user)).status_code==200
        assert (await client.patch('/documents/'+v1['document_id']+'/enabled',headers=admin,json={'enabled':False})).status_code==204
        events=await ask('如何连接？','disabled');evidence=next(d for k,d in events if k=='evidence');assert evidence['evidence']==[]
        assert events[-1][1]['status']=='refused'
        await client.patch('/documents/'+v1['document_id']+'/enabled',headers=admin,json={'enabled':True})
        # 同一会话并发生成必须被拒绝，取消后终态必须落库。
        async with client.stream('POST','/chat',headers=user,json={'session_id':session,'question':'慢速连接','request_id':'slow'}) as response:
            async for line in response.aiter_lines():
                if line.startswith('event: status'):
                    duplicate=await client.post('/chat',headers=user,json={'session_id':session,'question':'并发请求','request_id':'parallel'})
                    assert duplicate.status_code==409
                    break
        await asyncio.sleep(.5)
        history=(await client.get(f'/sessions/{session}/messages',headers=user)).json()
        assert len([m for m in history if m['request_id']=='first'])==1
        assert next(m for m in history if m['request_id']=='slow')['status']=='interrupted'
        assert (await client.get('/files/'+v1['version_id'])).status_code==401
        Path('../data/integration-context.json').write_text(json.dumps({'kb':kb,'document':v1['document_id'],'version':v2['version_id'],'session':session}),encoding='utf-8')
        print('集成验证通过：授权、上传、重复检测、失败保旧、版本发布、旧引用、停用双路排除、请求幂等、并发拒绝、中断持久化。')

if __name__=='__main__':asyncio.run(main())
