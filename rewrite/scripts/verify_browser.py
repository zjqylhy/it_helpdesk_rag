"""用一次性数据库验证真实 Python API 与全新 Vue 页面；所有凭据和内容均为 synthetic。"""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql

from helpdesk.adapters.postgres.migration import migrate
from helpdesk.entrypoints.admin import bootstrap

ROOT=Path(__file__).resolve().parents[2]
REWRITE=ROOT/'rewrite'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


def wait(url,process):
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError('测试服务启动失败，请查看独立 QA 日志')
        try:
            if httpx.get(url,timeout=1).status_code==200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(.1)
    raise RuntimeError('测试服务未就绪')


def main():
    state=json.loads((ROOT/'data/rewrite-m1-postgres/state.json').read_text())
    params={'host':'127.0.0.1','port':state['port'],'user':'postgres','password':state['password'],'dbname':'postgres'}
    name='m1test_'+uuid4().hex
    with psycopg.connect(**params,autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    dsn=psycopg.conninfo.make_conninfo(**{**params,'dbname':name})
    output=ROOT/'data/rewrite-m1-qa'
    output.mkdir(parents=True,exist_ok=True)
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    processes=[]
    logs=[]
    try:
        migrate(dsn,REWRITE/'db/migrations')
        if sys.platform=='win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(bootstrap(dsn,'qa-admin','QaAdminTest123!'))
        api_port,web_port=free_port(),free_port()
        env=os.environ.copy()
        env.update(HD_DATABASE_URL=dsn,HD_UPLOAD_DIR=str(output/'uploads'),HD_PORT=str(api_port),
                   HD_API_TARGET=f'http://127.0.0.1:{api_port}',HD_WEB_TARGET=f'http://127.0.0.1:{web_port}',
                   HD_QA_ARTIFACTS=str(output),PYTHONIOENCODING='utf-8')
        for label,command,cwd in [
            ('api',[sys.executable,'-m','helpdesk.entrypoints.api'],REWRITE/'backend'),
            ('web',['node',str(REWRITE/'frontend/node_modules/vite/bin/vite.js'),'--host','127.0.0.1','--port',str(web_port),'--strictPort'],REWRITE/'frontend')]:
            log=(output/(label+'.log')).open('wb');logs.append(log)
            processes.append(subprocess.Popen(command,cwd=cwd,env=env,creationflags=flags,stdout=log,stderr=log))
        wait(env['HD_API_TARGET']+'/health',processes[0]);wait(env['HD_WEB_TARGET'],processes[1])
        with httpx.Client(base_url=env['HD_API_TARGET']) as client:
            token=client.post('/api/v1/auth/login',json={'username':'qa-admin','password':'QaAdminTest123!'}).json()['token']
            headers={'Authorization':'Bearer '+token}
            client.post('/api/v1/admin/users',headers=headers,json={'username':'qa-user','password':'QaUserTest123!'}).raise_for_status()
            for title in ['公开知识库','受限知识库']:
                kb=client.post('/api/v1/knowledge-bases',headers=headers,json={'name':title,'organization':'合成测试学校'}).json()
                doc=client.post('/api/v1/documents',headers=headers,data={'kb_id':kb['id'],'title':'测试文档'},files={'file':('example.txt',title.encode())}).json()
                if title=='公开知识库':
                    client.put('/api/v1/admin/documents/'+doc['document_id']+'/access',headers=headers,
                               json={'visibility':'all_users','group_ids':[]}).raise_for_status()
        result=subprocess.run(['node',str(REWRITE/'scripts/browser_check.mjs')],env=env,cwd=ROOT,
                              creationflags=flags,capture_output=True,text=True,encoding='utf-8',timeout=90)
        (output/'browser.log').write_text(result.stdout+result.stderr,encoding='utf-8')
        if result.returncode:
            raise RuntimeError('浏览器验收失败，请查看 data/rewrite-m1-qa/browser.log')
        print(result.stdout.strip())
    finally:
        for process in reversed(processes):
            process.terminate();process.wait(timeout=10)
        for log in logs:
            log.close()
        with psycopg.connect(**params,autocommit=True) as conn:
            conn.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


if __name__=='__main__':
    main()
