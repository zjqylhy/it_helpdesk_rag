"""独立空库和本机替身服务验证工程契约，退出时释放本脚本启动的进程。"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import quote

from psycopg import sql
from postgres import connection, state

ROOT = Path(__file__).resolve().parents[1]


def main():
    name = 'effect_contract_' + uuid.uuid4().hex[:10]
    output = ROOT / 'data' / name; output.mkdir(parents=True)
    ports = [18180, 18100, 18110]
    for port in ports:
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1', port)) == 0:
                raise ValueError('独立测试端口已占用，拒绝接管已有服务')
    with connection() as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    native = state()
    dsn = f"postgresql://postgres:{quote(native['admin_password'], safe='')}@127.0.0.1:{native['port']}/{name}?sslmode=disable"
    env = {**os.environ, 'DATABASE_URL': dsn, 'INTERNAL_TOKEN': 'effect-depth-contract-token-123456789',
        'ADMIN_USER': 'admin', 'ADMIN_PASSWORD': 'test-admin-password', 'USER_NAME': 'demo', 'USER_PASSWORD': 'test-user-password',
        'DEEPSEEK_BASE_URL': 'http://127.0.0.1:18110', 'DASHSCOPE_BASE_URL': 'http://127.0.0.1:18110',
        'DEEPSEEK_API_KEY': 'contract-test', 'DASHSCOPE_API_KEY': 'contract-test', 'EMBEDDING_DIMENSION': '2',
        'CHAT_GATEWAY_ENABLED': 'false', 'RETRIEVAL_GATEWAY_ENABLED': 'false', 'MODEL_GATEWAY_CA_FILE': '',
        'HTTP_ADDR': '127.0.0.1:18180', 'RAG_URL': 'http://127.0.0.1:18100', 'RAG_PORT': '18100',
        'TEST_API_URL': 'http://127.0.0.1:18180/api/v1', 'TEST_RAG_URL': 'http://127.0.0.1:18100',
        'UPLOAD_DIR': str(output / 'uploads'), 'EFFECT_CHECK_ROOT': str(output), 'PYTHONUTF8': '1', 'NO_PROXY': '127.0.0.1,localhost'}
    processes, files, checks = [], [], []
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

    def run(args, cwd):
        r = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, encoding='utf-8', creationflags=flags, timeout=120)
        if r.returncode:
            raise RuntimeError(r.stdout + r.stderr)
        print(r.stdout.strip(), flush=True)

    def start(args, cwd, label):
        f = (output / (label + '.log')).open('w', encoding='utf-8'); files.append(f)
        p = subprocess.Popen(args, cwd=cwd, env=env, stdout=f, stderr=f, creationflags=flags)
        processes.append(p)

    def health(url):
        for _ in range(100):
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    if response.status == 200:
                        return
            except OSError:
                if any(p.poll() is not None for p in processes):
                    raise RuntimeError('测试服务退出，请检查本次私有日志')
                time.sleep(.2)
        raise RuntimeError('独立服务健康检查超时')

    try:
        binary = output / ('backend.exe' if os.name == 'nt' else 'backend')
        run(['go', 'build', '-o', str(binary), '.'], ROOT / 'backend')
        run([sys.executable, '-m', 'tests.legacy_seed'], ROOT / 'rag')
        start([str(binary)], ROOT / 'backend', 'go')
        health('http://127.0.0.1:18180/health')
        start([sys.executable, '-m', 'uvicorn', 'tests.fake_models:app', '--host', '127.0.0.1', '--port', '18110'], ROOT / 'rag', 'models')
        health('http://127.0.0.1:18110/openapi.json')
        start([sys.executable, '-m', 'helpdesk'], ROOT / 'rag', 'rag')
        health('http://127.0.0.1:18100/health')
        for module in ['tests.scope_evaluation_check', 'tests.scope_integration_check', 'tests.access_integration_check']:
            run([sys.executable, '-m', module], ROOT / 'rag'); checks.append(module)
        (output / 'verification.json').write_text(json.dumps({'database': name, 'checks': checks, 'external_model_calls': 0}, indent=2), encoding='utf-8')
        print(f'独立契约通过：{name}；所有模型请求仅访问本机替身。', flush=True)
    finally:
        for p in reversed(processes):
            p.terminate()
        for p in processes:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill(); p.wait()
        for f in files:
            f.close()


if __name__ == '__main__':
    main()
