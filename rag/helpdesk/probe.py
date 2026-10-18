"""少量真实模型协议联调：最多五次 HTTP 请求，不输出 Key、向量或回答原文。"""
import asyncio
import json
import time

from .config import Settings
from .providers import Models, ProviderError
from .runtime import loop_factory


async def main():
    settings = Settings(model_timeout=45)
    if (not (settings.gateway_api_key if settings.chat_gateway_enabled else settings.deepseek_api_key)
            or not (settings.gateway_api_key if settings.retrieval_gateway_enabled else settings.dashscope_api_key)):
        print('模型 Key 未完全配置；未发出请求。')
        return 1
    models = Models(settings)
    models.max_calls = 5
    documents = ['工程联调素材：客户端连接前填写组织提供的服务器地址。', '工程联调素材：打印机缺纸时应补充纸张。']
    results = []

    async def check(name, operation):
        start = time.monotonic()
        try:
            details = await operation()
            results.append({'check': name, 'status': 'passed', 'seconds': round(time.monotonic() - start, 2), **details})
        except ProviderError as error:
            # 适配器已将第三方原始错误转为无凭据的分类。
            results.append({'check': name, 'status': 'failed', 'error': str(error)})

    async def embedding(texts, kind):
        vectors = await models.embed(texts, kind)
        return {'count': len(vectors), 'dimension': len(vectors[0])}

    async def rerank():
        rows = await models.rerank('如何连接客户端？', documents, top_n=2)
        return {'count': len(rows), 'top_index': rows[0]['index']}

    async def structured():
        answer = await models.json('协议联调，请仅返回 {"ok":true}。', {'check': 'json'})
        if answer.get('ok') is not True:
            raise ProviderError('结构化联调结果不符合预期')
        return {'json_valid': True}

    async def streaming():
        chunks = [chunk async for chunk in models.stream([{'role': 'user', 'content': '协议联调，仅回答：联调成功。'}])]
        length = sum(len(chunk['text']) for chunk in chunks)
        if not length:
            raise ProviderError('流式联调未产生正文')
        return {'characters': length, 'terminal_valid': True}

    try:
        await check('embedding_document', lambda: embedding(documents, 'document'))
        await check('embedding_query', lambda: embedding(['如何连接客户端？'], 'query'))
        await check('rerank', rerank)
        await check('llm_json', structured)
        await check('llm_stream', streaming)
        print(json.dumps({'results': results, 'http_calls': models.calls, 'usage': models.usage}, ensure_ascii=False, indent=2))
        return 0 if all(item['status'] == 'passed' for item in results) else 1
    finally:
        await models.close()


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main(), loop_factory=loop_factory))
