import asyncio
import json
from .config import Settings
from .providers import Models
from .runtime import loop_factory

async def probe():
    settings=Settings()
    if not settings.deepseek_api_key or not settings.dashscope_api_key:
        raise SystemExit('请先在本地 .env 配置两家 API Key；不要在聊天中发送凭据。')
    models=Models(settings);models.max_calls=8
    try:
        vectors=await models.embed(['用于接口测试的非敏感文本：客户端连接配置。'])
        ranks=await models.rerank('客户端如何配置？',['先打开客户端，再配置服务器。','今天是晴天。'],top_n=2)
        await models.json('返回 {"ok":true}，用于接口测试。',{})
        answer=''
        async for delta in models.stream([{'role':'user','content':'请只回复“接口测试成功”。'}]):answer+=delta['text']
        print(json.dumps({'embedding_dimension':len(vectors[0]),'rerank_count':len(ranks),'stream_completed':bool(answer),
                          'calls':models.calls,'usage':models.ledger or None},ensure_ascii=False,indent=2))
    finally:await models.close()

if __name__=='__main__':asyncio.run(probe(),loop_factory=loop_factory)
