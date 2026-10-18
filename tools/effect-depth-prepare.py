"""在独立数据库副本应用当前 schema，原生业务库只作为复制来源。"""
import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

import psycopg
from psycopg import sql

from postgres import connection, state
from helpdesk.config import Settings
from helpdesk.access import AuthorizedStore
from helpdesk.evaluation.scope_store import export_scope, FrozenScopeStore
from helpdesk.runtime import loop_factory

ROOT = Path(__file__).resolve().parents[1]


async def export(settings, target):
    rows = await AuthorizedStore(settings, '', 0).rows("SELECT id FROM users WHERE role='user' AND NOT disabled ORDER BY username LIMIT 1")
    if not rows:
        raise ValueError('副本缺少普通用户评测主体')
    revision = (await AuthorizedStore(settings, '', 0).rows('SELECT revision FROM authorization_state WHERE id=1'))[0]['revision']
    store = AuthorizedStore(settings, rows[0]['id'], revision)
    bundle = await export_scope(store)
    await FrozenScopeStore(settings, bundle).verify()
    (target / 'corpus.json').write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding='utf-8')
    return len(bundle['members']), len(bundle['corpus']['chunks'])


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--out', required=True)
    args = parser.parse_args(); target = Path(args.out).resolve()
    if not target.is_relative_to(ROOT / 'eval/private') or target.exists():
        raise ValueError('输出必须是 eval/private 下的新目录')
    settings = Settings(); source = urlsplit(settings.database_url).path.lstrip('/')
    project_state = state()
    address = urlsplit(settings.database_url)
    if address.hostname not in ('localhost', '127.0.0.1') or address.port != project_state['port']:
        raise ValueError('只能复制本项目原生数据库，拒绝远程数据库')
    name = 'effect_depth_' + uuid.uuid4().hex[:10]
    with connection() as admin:
        admin.execute(sql.SQL('CREATE DATABASE {} TEMPLATE {}').format(sql.Identifier(name), sql.Identifier(source)))
    dsn = f"postgresql://postgres:{quote(project_state['admin_password'], safe='')}@127.0.0.1:{project_state['port']}/{name}?sslmode=disable"
    with psycopg.connect(dsn, autocommit=True) as copied:
        copied.execute((ROOT / 'db/schema.sql').read_text(encoding='utf-8'))
    target.mkdir(parents=True)
    # 此文件含本地连接凭据，只能保存在已忽略的私有目录。
    (target / 'environment.json').write_text(json.dumps({'database_url': dsn, 'database': name, 'source': source}, indent=2), encoding='utf-8')
    members, chunks = asyncio.run(export(settings.model_copy(update={'database_url': dsn}), target), loop_factory=loop_factory)
    print(f'独立副本 {name}：{members} 库、{chunks} 切片；原库未迁移，未调用模型。')


if __name__ == '__main__':
    main()
