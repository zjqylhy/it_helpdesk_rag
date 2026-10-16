"""隔离的本机 PostgreSQL 实例；只操作指定测试目录，不读取业务库凭据。"""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time

import psycopg

ROOT=Path(__file__).resolve().parents[2]
TEST_ROOT=ROOT/'data/rewrite-m1-postgres'
BIN=ROOT/'data/native-postgres/runtime/Library/bin'


def main() -> None:
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['start','stop'])
    args=parser.parse_args()
    TEST_ROOT.mkdir(parents=True,exist_ok=True)
    cluster=(TEST_ROOT/'cluster').resolve()
    if not cluster.is_relative_to((ROOT/'data').resolve()):
        raise RuntimeError('测试目录越界')
    env=os.environ.copy()
    env['PATH']=str(BIN)+os.pathsep+env.get('PATH','')
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    state_path=TEST_ROOT/'state.json'
    if not state_path.exists():
        state_path.write_text(json.dumps({'port':55439,'password':secrets.token_hex(24)}),encoding='utf-8')
    state=json.loads(state_path.read_text(encoding='utf-8'))
    if args.action=='stop':
        subprocess.run([str(BIN/'pg_ctl.exe'),'stop','-D',str(cluster),'-m','fast','-w'],env=env,
                       creationflags=flags,check=True,capture_output=True)
        print('M1 独立测试数据库已停止')
        return
    if not (cluster/'PG_VERSION').exists():
        pw=TEST_ROOT/'init-password.tmp'
        pw.write_text(state['password'],encoding='utf-8')
        try:
            subprocess.run([str(BIN/'initdb.exe'),'-D',str(cluster),'-U','postgres','--auth=scram-sha-256',
                            '--encoding=UTF8','--locale=C','--pwfile',str(pw)],env=env,creationflags=flags,
                           check=True,capture_output=True)
        finally:
            pw.unlink(missing_ok=True)
    dsn=f"host=127.0.0.1 port={state['port']} user=postgres password={state['password']} dbname=postgres"
    try:
        with psycopg.connect(dsn,connect_timeout=2):
            print('M1 独立测试数据库已运行')
            return
    except psycopg.OperationalError:
        pass
    # Windows 可能保留一段动态端口；只为未运行的本测试集群选择可绑定端口。
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0))
        state['port']=probe.getsockname()[1]
    state_path.write_text(json.dumps(state),encoding='utf-8')
    dsn=f"host=127.0.0.1 port={state['port']} user=postgres password={state['password']} dbname=postgres"
    with (TEST_ROOT/'postgres.log').open('ab') as log:
        process=subprocess.Popen([str(BIN/'postgres.exe'),'-D',str(cluster),'-h','127.0.0.1','-p',str(state['port'])],
                                 env=env,creationflags=flags,stdin=subprocess.DEVNULL,stdout=log,stderr=log)
    for _ in range(40):
        if process.poll() is not None:
            raise RuntimeError('独立测试数据库启动失败，请检查本地日志')
        try:
            with psycopg.connect(dsn,connect_timeout=1):
                print('M1 独立 PostgreSQL 已启动，端口 '+str(state['port']))
                return
        except psycopg.OperationalError:
            time.sleep(.25)
    raise RuntimeError('独立测试数据库启动超时')


if __name__=='__main__':
    main()
