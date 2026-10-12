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
    chat_gateway = value('CHAT_GATEWAY_ENABLED').lower() in ('true','1','yes','on')
    retrieval_gateway = value('RETRIEVAL_GATEWAY_ENABLED').lower() in ('true','1','yes','on')
    ready = True
    required = ['INTERNAL_TOKEN','ADMIN_USER','ADMIN_PASSWORD','USER_NAME','USER_PASSWORD']
    if not chat_gateway: required.append('DEEPSEEK_API_KEY')
    if not retrieval_gateway: required.append('DASHSCOPE_API_KEY')
    if chat_gateway or retrieval_gateway: required.append('IT_HELPDESK_RAG_API_KEY')
    for key in required:
        item = value(key)
        if key == 'DASHSCOPE_API_KEY': item = item or value('QWEN_API_KEY')
        valid = bool(item) and not item.startswith('replace-with-')
        if key == 'INTERNAL_TOKEN': valid = valid and len(item) >= 24
        if key.endswith('_PASSWORD'): valid = valid and 12 <= len(item.encode()) <= 72
        print(f'{key}：{"已配置" if valid else "需填写有效值"}')
        ready = ready and valid
    bases = []
    if chat_gateway or retrieval_gateway:
        bases.append(('模型网关地址', value('MODEL_GATEWAY_BASE_URL') or 'https://localhost:9443/v1', True))
    if not retrieval_gateway:
        bases.append(('阿里业务空间地址', value('DASHSCOPE_BASE_URL'), False))
    for label, base, gateway in bases:
        parsed = urlsplit(base)
        valid_base = (parsed.scheme == 'https' and bool(parsed.hostname)
                      and (parsed.path.rstrip('/') == '/v1' if gateway else parsed.path in ('','/'))
                      and not parsed.query and not parsed.fragment and not parsed.username
                      and 'your-workspace' not in base)
        print(f'{label}：{"已配置" if valid_base else "需填写有效 HTTPS 地址"}')
        ready = ready and valid_base
    if (chat_gateway or retrieval_gateway) and value('MODEL_GATEWAY_CA_FILE'):
        valid_ca = Path(value('MODEL_GATEWAY_CA_FILE')).is_file()
        print(f'网关 CA 文件：{"已配置" if valid_ca else "文件不存在"}')
        ready = ready and valid_ca
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
