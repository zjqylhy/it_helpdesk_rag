"""项目目录内的 Windows 原生数据库管理；不注册系统服务，不调用模型。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tarfile
import time
import urllib.request
from urllib.parse import quote, urlsplit
import psycopg
from psycopg import sql
from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / 'data/native-postgres'
RUNTIME = NATIVE / 'runtime'
DATA = NATIVE / 'database'
STATE = NATIVE / 'credentials.json'
MICRO_URL = 'https://api.anaconda.org/download/conda-forge/micromamba/2.9.0/win-64/micromamba-2.9.0-0.tar.bz2'
MICRO_HASH = '97a336f4ab794bd96a6a4da5e6ed63e75a1d31830414a182419b23d3b36f3fe0'

def environment():
    result = os.environ.copy()
    result['PATH'] = str(RUNTIME / 'Library/bin') + os.pathsep + str(RUNTIME / 'bin') + os.pathsep + result.get('PATH','')
    return result

def run(program, *args, capture=False):
    result = subprocess.run([str(program), *map(str,args)], env=environment(),
        creationflags=subprocess.CREATE_NO_WINDOW, capture_output=capture, text=capture, encoding='utf-8' if capture else None)
    if result.returncode:
        raise RuntimeError(f'{Path(program).name} 失败（退出码 {result.returncode}）')
    return result

def binary(name):
    path = RUNTIME / 'Library/bin' / (name+'.exe')
    if not path.is_file():
        raise RuntimeError('原生数据库未安装，先运行 postgres.py install')
    return path

def install():
    NATIVE.mkdir(parents=True,exist_ok=True)
    if (RUNTIME/'Library/bin/pg_ctl.exe').is_file():
        print('原生数据库运行包已存在，不重复安装。')
        return
    micro = NATIVE/'micromamba.exe'
    archive = NATIVE/'micromamba-2.9.0.tar.bz2'
    if not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest()!=MICRO_HASH:
        with urllib.request.urlopen(MICRO_URL,timeout=120) as response:
            content=response.read()
        if hashlib.sha256(content).hexdigest()!=MICRO_HASH:
            raise RuntimeError('Micromamba 下载校验失败')
        archive.write_bytes(content)
    with tarfile.open(archive,'r:bz2') as package:
        # 只提取固定成员，避免依赖包路径越出项目目录。
        executable=package.extractfile('Library/bin/micromamba.exe')
        if executable is None:raise RuntimeError('安装包缺少 Micromamba 可执行文件')
        micro.write_bytes(executable.read())
    result=run(micro,'--no-rc','create','--root-prefix',NATIVE/'mamba','--prefix',RUNTIME,
        '--channel','conda-forge','--strict-channel-priority','postgresql=16.15','pgvector=0.8.6','--yes','--json',capture=True)
    manifest=json.loads(result.stdout)
    (NATIVE/'install-result.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    if not manifest.get('success'):raise RuntimeError('数据库依赖安装失败')
    print('PostgreSQL 16.15 + pgvector 0.8.6 已安装到项目 data/native-postgres。')

def state():
    if not STATE.is_file():raise RuntimeError('数据库尚未初始化，先运行 postgres.py init')
    return json.loads(STATE.read_text(encoding='utf-8'))

def running():
    if not (DATA/'PG_VERSION').is_file():return False
    result=subprocess.run([str(binary('pg_ctl')),'status','-D',str(DATA)],env=environment(),
        creationflags=subprocess.CREATE_NO_WINDOW,capture_output=True)
    return result.returncode==0

def connection(db='postgres'):
    data=state()
    return psycopg.connect(host='127.0.0.1',port=data['port'],user='postgres',password=data['admin_password'],
                           dbname=db,connect_timeout=3,autocommit=True)

def start():
    if running():
        print('项目数据库已在运行。')
        return
    data=state()
    log_path=NATIVE/'postgres.log'
    with log_path.open('ab') as log:
        process=subprocess.Popen([str(binary('postgres')),'-D',str(DATA),'-h','127.0.0.1','-p',str(data['port'])],
            env=environment(),creationflags=subprocess.CREATE_NO_WINDOW,stdin=subprocess.DEVNULL,stdout=log,stderr=log)
    for _ in range(60):
        if process.poll() is not None:raise RuntimeError('数据库启动失败，请检查 data/native-postgres/postgres.log')
        try:
            with connection():pass
            print(f"项目数据库已启动：127.0.0.1:{data['port']}。")
            return
        except psycopg.Error:time.sleep(.5)
    raise RuntimeError('数据库启动超时，请检查本地 postgres.log')

def stop():
    if not running():
        print('项目数据库未运行。')
        return
    run(binary('pg_ctl'),'stop','-D',DATA,'-m','fast','-w','-t','30',capture=True)
    print('项目数据库已正常停止，原文件和数据保留。')

def init(port):
    binary('initdb')
    if (DATA/'PG_VERSION').is_file():
        print('项目数据库已初始化，保留现有数据和凭据。')
        data=state()
        port=data['port']
    else:
        if DATA.exists() and any(DATA.iterdir()):raise RuntimeError('数据库目录非空，拒绝重新初始化，请检查目录')
        data={'port':port,'admin_password':secrets.token_hex(24),'business_password':secrets.token_hex(24)}
        NATIVE.mkdir(parents=True,exist_ok=True)
        STATE.write_text(json.dumps(data,indent=2),encoding='utf-8')
        password_file=NATIVE/'init-password.tmp'
        password_file.write_text(data['admin_password'],encoding='utf-8')
        try:
            run(binary('initdb'),'-D',DATA,'-U','postgres','--auth=scram-sha-256','--encoding=UTF8','--locale=C','--pwfile',password_file,capture=True)
        finally:password_file.unlink(missing_ok=True)
    start()
    # 初始化中断后可续做建库步骤；已有业务库、账号和数据不重建。
    with connection() as conn:
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='rag'").fetchone():
            conn.execute(sql.SQL('CREATE ROLE rag LOGIN NOSUPERUSER NOCREATEDB PASSWORD {}').format(sql.Literal(data['business_password'])))
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname='rag'").fetchone():
            conn.execute('CREATE DATABASE rag OWNER rag')
    with connection('rag') as conn:
        conn.execute('CREATE EXTENSION IF NOT EXISTS vector')
    url=f"postgresql://rag:{quote(data['business_password'],safe='')}@127.0.0.1:{port}/rag?sslmode=disable"
    config=ROOT/'.env'
    values=dotenv_values(config) if config.is_file() else {}
    old_url=values.get('DATABASE_URL') or ''
    parsed=urlsplit(old_url)
    sample='postgresql://rag:rag@localhost:5432/rag?sslmode=disable'
    owned=parsed.hostname=='127.0.0.1' and parsed.port==port and parsed.path=='/rag' and parsed.username=='rag'
    if not old_url or old_url==sample or owned:
        if not config.is_file():config.write_text((ROOT/'.env.example').read_text(encoding='utf-8'),encoding='utf-8')
        set_key(str(config),'DATABASE_URL',url,quote_mode='never')
        set_key(str(config),'POSTGRES_PASSWORD',data['business_password'],quote_mode='never')
        print('已更新 .env 的本地数据库连接，其他配置保留。')
    else:
        print('当前 DATABASE_URL 是自定义值，未覆盖；项目数据库凭据保存在忽略的 credentials.json 中。')
    print('业务库 rag 与 pgvector 已就绪。')

def status():
    if not running():print('项目数据库未运行。');return 1
    with connection('rag') as conn:
        version=conn.execute('SHOW server_version').fetchone()[0]
        extension=conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()
    print(json.dumps({'postgresql':version,'pgvector':extension[0] if extension else None,'port':state()['port']},ensure_ascii=False))
    return 0

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['install','init','start','stop','status'])
    parser.add_argument('--port',type=int,default=55433)
    args=parser.parse_args()
    if os.name!='nt':raise RuntimeError('此安装入口仅用于 Windows')
    if not 1024<=args.port<=65535:raise ValueError('端口范围应为 1024–65535')
    if args.action=='install':install()
    elif args.action=='init':init(args.port)
    elif args.action=='start':start()
    elif args.action=='stop':stop()
    else:return status()
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except (RuntimeError,ValueError) as error:raise SystemExit(str(error))
    except psycopg.Error:raise SystemExit('数据库连接或建库失败，请检查本地配置和 postgres.log；未输出凭据。')
