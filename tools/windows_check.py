"""Windows 本地启动诊断：只检查配置和数据库，不调用模型或打印凭据。"""
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
from dotenv import dotenv_values
import psycopg

ROOT = Path(__file__).resolve().parents[1]

def main():
    path = ROOT / '.env'
    if not path.is_file():
        print('.env：尚未创建，请运行 tools/windows.ps1 -Action Init。')
        return 1
    values = dotenv_values(path)
    def value(key):
        return os.environ.get(key, values.get(key) or '')
    ready = True
    for key in ('INTERNAL_TOKEN','ADMIN_USER','ADMIN_PASSWORD','USER_NAME','USER_PASSWORD','DEEPSEEK_API_KEY','DASHSCOPE_API_KEY'):
        item = value(key)
        if key == 'DASHSCOPE_API_KEY': item = item or value('QWEN_API_KEY')
        valid = bool(item) and not item.startswith('replace-with-')
        if key == 'INTERNAL_TOKEN': valid = valid and len(item) >= 24
        if key.endswith('_PASSWORD'): valid = valid and 12 <= len(item.encode()) <= 72
        print(f'{key}：{"已配置" if valid else "需填写有效值"}')
        ready = ready and valid
    base = value('DASHSCOPE_BASE_URL')
    parsed = urlsplit(base)
    valid_base = (parsed.scheme == 'https' and bool(parsed.hostname) and parsed.path in ('', '/')
                  and not parsed.query and not parsed.fragment and not parsed.username
                  and 'your-workspace' not in base)
    print(f'阿里业务空间地址：{"已配置" if valid_base else "需填写 HTTPS 主机根地址"}')
    ready = ready and valid_base
    dsn = value('DATABASE_URL')
    if not dsn:
        print('数据库：请填写 DATABASE_URL。')
        return 1
    try:
        with psycopg.connect(dsn, connect_timeout=5, options='-c statement_timeout=5000') as connection:
            available = connection.execute("SELECT EXISTS(SELECT 1 FROM pg_available_extensions WHERE name='vector')").fetchone()[0]
            enabled = connection.execute("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector')").fetchone()[0]
        print('PostgreSQL：连接成功。')
        print('pgvector：' + ('已启用。' if enabled else '已安装，需在目标库启用。' if available else '未安装扩展。'))
        ready = ready and enabled
    except psycopg.Error:
        print('PostgreSQL：连接失败，请检查数据库服务、DATABASE_URL 和账号权限。')
        ready = False
    return 0 if ready else 1

if __name__ == '__main__':
    sys.exit(main())
