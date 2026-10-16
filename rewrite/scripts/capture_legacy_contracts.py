"""只读旧程序的外部行为；所有写入落到一次性的 synthetic 测试库。"""
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql

ROOT=Path(__file__).resolve().parents[2]


def main() -> None:
    state=json.loads((ROOT/'data/rewrite-m1-postgres/state.json').read_text())
    params={'host':'127.0.0.1','port':state['port'],'user':'postgres','password':state['password'],'dbname':'postgres'}
    name='m1legacy_'+uuid4().hex
    with psycopg.connect(**params,autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0))
        port=probe.getsockname()[1]
    env=os.environ.copy()
    folder=ROOT/'data/rewrite-legacy-contracts'
    folder.mkdir(exist_ok=True,parents=True)
    env.update(DATABASE_URL=psycopg.conninfo.make_conninfo(**{**params,'dbname':name}),
        HTTP_ADDR=f'127.0.0.1:{port}',UPLOAD_DIR=str(folder/'files'),RAG_URL='http://127.0.0.1:9',
        INTERNAL_TOKEN='contract-test-only-token-48-characters-long-for-local',ADMIN_USER='contract-admin',ADMIN_PASSWORD='ContractAdmin123!',
        USER_NAME='contract-user',USER_PASSWORD='ContractUser123!')
    process=None
    try:
        with (folder/'legacy.log').open('ab') as log:
            process=subprocess.Popen([str(ROOT/'data/rewrite-legacy-contract-api.exe')],env=env,cwd=ROOT/'backend',
                stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        client=httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=3)
        for _ in range(80):
            if process.poll() is not None:
                raise RuntimeError('旧对照服务退出，请查看隔离目录日志')
            try:
                if client.get('/health').status_code==200:
                    break
            except httpx.ConnectError:
                pass
            time.sleep(.1)
        samples=[]
        def request(label,method,path,**kwargs):
            response=client.request(method,path,**kwargs)
            try:
                body=response.json()
            except ValueError:
                body=response.text
            public=body.copy() if isinstance(body,dict) else body
            if isinstance(public,dict) and 'token' in public:
                public['token']='<redacted>'
            samples.append({'label':label,'method':method,'path':path,'status':response.status_code,'body':public})
            return body
        login=request('login','POST','/api/v1/auth/login',json={'username':'contract-admin','password':'ContractAdmin123!'})
        headers={'Authorization':'Bearer '+login['token']}
        request('me','GET','/api/v1/auth/me',headers=headers)
        request('unauthenticated','GET','/api/v1/knowledge-bases')
        kb=request('create_kb','POST','/api/v1/knowledge-bases',headers=headers,json={'name':'合约库','organization':'合约学校'})
        request('list_kbs','GET','/api/v1/knowledge-bases',headers=headers)
        request('list_documents','GET','/api/v1/documents',headers=headers)
        upload=request('upload','POST','/api/v1/documents',headers=headers,data={'kb_id':kb['id'],'title':'合约文档'},
                       files={'file':('sample.txt',b'contract example')})
        request('list_documents_after_upload','GET','/api/v1/documents',headers=headers)
        request('list_documents_in_kb','GET','/api/v1/documents?kb_id='+kb['id'],headers=headers)
        # 首先记录外部响应形状，后续适配器不根据旧源码推断。
        document_id=upload.get('document_id') or upload.get('id')
        if document_id:
            request('versions','GET',f'/api/v1/documents/{document_id}/versions',headers=headers)
            request('enabled','PATCH',f'/api/v1/documents/{document_id}/enabled',headers=headers,json={'enabled':False})
        request('create_session','POST','/api/v1/sessions',headers=headers,json={'kb_ids':[kb['id']]})
        request('list_sessions','GET','/api/v1/sessions',headers=headers)
        request('admin_users','GET','/api/v1/admin/users',headers=headers)
        request('create_user','POST','/api/v1/admin/users',headers=headers,
                json={'username':'new-user','password':'ContractNewUser123!','role':'user','usage_kind':'demo'})
        request('admin_groups','GET','/api/v1/admin/groups',headers=headers)
        request('create_group','POST','/api/v1/admin/groups',headers=headers,json={'name':'合约组'})
        groups=request('groups_after_create','GET','/api/v1/admin/groups',headers=headers)
        users=request('users_after_create','GET','/api/v1/admin/users',headers=headers)
        created_user=next(u for u in users if u['username']=='new-user')
        request('members','PUT',f"/api/v1/admin/groups/{groups[0]['id']}/members",headers=headers,json={'user_ids':[created_user['id']]})
        request('disabled','PATCH',f"/api/v1/admin/users/{created_user['id']}/disabled",headers=headers,json={'disabled':True})
        request('update_user','PATCH',f"/api/v1/admin/users/{created_user['id']}",headers=headers,json={'disabled':True,'usage_kind':'demo'})
        request('default_search','PATCH',f"/api/v1/knowledge-bases/{kb['id']}/default-search",headers=headers,json={'default_search':False})
        request('default_search_enabled','PATCH',f"/api/v1/knowledge-bases/{kb['id']}/default-search",headers=headers,json={'enabled':False})
        request('real_usage','POST','/api/v1/admin/users',headers=headers,json={'username':'real-account','password':'ContractReal123!','role':'user','usage_kind':'real_usage'})
        request('invalid_usage','POST','/api/v1/admin/users',headers=headers,json={'username':'invalid-account','password':'ContractReal123!','role':'user','usage_kind':'invalid'})
        for kind in ['test','eval','benchmark','real']:
            request('usage_'+kind,'POST','/api/v1/admin/users',headers=headers,json={'username':'kind-'+kind,'password':'ContractReal123!','role':'user','usage_kind':kind})
        if document_id:
            request('access','GET',f'/api/v1/admin/documents/{document_id}/access',headers=headers)
            request('set_access','PUT',f'/api/v1/admin/documents/{document_id}/access',headers=headers,
                    json={'visibility':'all_users','group_ids':[]})
            request('set_access_compat','PUT',f'/api/v1/documents/{document_id}/access',headers=headers,
                    json={'visibility':'all_users','group_ids':[]})
        with psycopg.connect(**{**params,'dbname':name}) as conn:
            conn.execute("UPDATE ingestion_jobs SET status='failed'")
            conn.execute("UPDATE document_versions SET status='failed'")
        request('retry','POST',f"/api/v1/ingestion-jobs/{upload['job_id']}/retry",headers=headers)
        request('logout','POST','/api/v1/auth/logout',headers=headers)
        output={'source_revision':'ceeba43','data':'synthetic-only','samples':samples}
        (folder/'responses.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
        for s in samples:
            print(s['label'],s['status'],list(s['body']) if isinstance(s['body'],dict) else type(s['body']).__name__)
    finally:
        if process is not None:
            process.terminate()
            process.wait(timeout=10)
        with psycopg.connect(**params,autocommit=True) as conn:
            conn.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


if __name__=='__main__':
    main()
