"""仅向空契约数据库写首期结构，验证升级不会将旧文档误设为私有。"""
import asyncio
from pathlib import Path
from helpdesk.config import Settings
from helpdesk.store import Store
from helpdesk.runtime import loop_factory


async def main():
    store=Store(Settings())
    async with await store.connect() as conn:
        row=await (await conn.execute("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema='public'")).fetchone()
        if row['n']:
            raise RuntimeError('Legacy fixture requires an empty isolated database')
        schema=(Path(__file__).resolve().parents[2]/'db/schema.sql').read_text(encoding='utf-8')
        await conn.execute(schema.split('-- 一次性迁移旧资料')[0])
        await conn.execute("INSERT INTO knowledge_bases(id,name,organization,default_search) VALUES('legacy-kb','历史迁移夹具','虚构迁移组织',false)")
        await conn.execute("INSERT INTO documents(id,kb_id,title) VALUES('legacy-doc','legacy-kb','历史文档夹具')")
    print('Legacy schema fixture prepared in empty isolated database.')


if __name__=='__main__':asyncio.run(main(),loop_factory=loop_factory)
