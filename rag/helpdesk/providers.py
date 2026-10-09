import asyncio
import json
import math
from collections.abc import AsyncIterator
import httpx
from .config import Settings

class ProviderError(RuntimeError):
    pass

class Models:
    def __init__(self, settings: Settings, transport=None):
        self.s = settings
        self.http = httpx.AsyncClient(timeout=settings.model_timeout, transport=transport)
        self.online = asyncio.Semaphore(3)
        self.embeddings_limit = asyncio.Semaphore(2)
        self.usage: list[dict] = []
        self.ledger: list[dict] = []
        self.calls = 0
        self.max_calls = None

    def count_call(self):
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise ProviderError('模型调用预算已用完')
        self.calls += 1

    async def close(self):
        await self.http.aclose()

    def headers(self, kind):
        key = self.s.deepseek_api_key if kind == 'llm' else self.s.dashscope_api_key
        if not key:
            raise ProviderError(f'{kind} API Key 未配置')
        return {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}

    async def post(self, path, body, kind):
        base = self.s.deepseek_base_url if kind == 'llm' else self.s.dashscope_base_url
        if not base.startswith(('https://', 'http://127.0.0.1', 'http://localhost')):
            raise ProviderError('模型服务地址需配置 HTTPS')
        for attempt in range(3):
            self.count_call()
            try:
                response = await self.http.post(base.rstrip('/') + path, json=body, headers=self.headers(kind))
            except httpx.HTTPError:
                raise ProviderError(f'{kind} 网络请求失败') from None
            if response.status_code in (429, 502, 503, 504) and attempt < 2:
                retry = response.headers.get('Retry-After', '')
                delay = min(float(retry), 10) if retry.replace('.', '', 1).isdigit() else 2 ** attempt
                await asyncio.sleep(delay)
                continue
            if response.is_error:
                # 提供商原始错误可能包含请求内容，公共响应只输出分类和状态码。
                raise ProviderError(f'{kind} HTTP {response.status_code}')
            try:
                data = response.json()
            except ValueError:
                raise ProviderError(f'{kind} 返回非 JSON') from None
            if data.get('code') or data.get('error'):
                raise ProviderError(f'{kind} 业务错误')
            if data.get('usage') is not None:
                usage = {'kind': kind, **data['usage']}
                self.usage.append(usage)
                self.ledger.append(usage)
            return data
        raise ProviderError('重试耗尽')

    async def embed(self, texts: list[str], text_type='document') -> list[list[float]]:
        result = []
        async with self.embeddings_limit:
            for start in range(0, len(texts), 20):
                batch = texts[start:start + 20]
                data = await self.post('/api/v1/services/embeddings/text-embedding/text-embedding', {
                    'model': self.s.embedding_model, 'input': {'texts': batch},
                    'parameters': {'text_type': text_type, 'dimension': self.s.embedding_dimension, 'output_type': 'dense'}}, 'embedding')
                rows = data.get('output', {}).get('embeddings', [])
                aligned = {}
                for row in rows:
                    index, vector = row.get('text_index'), row.get('embedding')
                    if type(index) is not int or index in aligned or not 0 <= index < len(batch):
                        raise ProviderError('Embedding 索引无效')
                    if not isinstance(vector, list) or len(vector) != self.s.embedding_dimension or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector):
                        raise ProviderError('Embedding 维度或数值无效')
                    if not any(vector):
                        raise ProviderError('Embedding 返回零向量')
                    aligned[index] = vector
                if len(aligned) != len(batch):
                    raise ProviderError('Embedding 数量不匹配')
                result.extend(aligned[i] for i in range(len(batch)))
        return result

    async def rerank(self, query, documents, top_n=5):
        if not documents:
            return []
        data = await self.post('/api/v1/services/rerank/text-rerank/text-rerank', {
            'model': self.s.rerank_model, 'input': {'query': query, 'documents': documents},
            'parameters': {'top_n': min(top_n, len(documents))}}, 'rerank')
        rows = data.get('output', {}).get('results', [])
        seen = set()
        for row in rows:
            idx, score = row.get('index'), row.get('relevance_score')
            if type(idx) is not int or idx in seen or not 0 <= idx < len(documents) or type(score) not in (int, float) or not math.isfinite(score):
                raise ProviderError('重排索引或分数无效')
            seen.add(idx)
        if len(rows) != min(top_n, len(documents)):
            raise ProviderError('重排结果数量不匹配')
        return sorted(rows, key=lambda x: x['relevance_score'], reverse=True)

    def llm_body(self, messages, stream=False, structured=False):
        body = {'model': self.s.llm_model, 'messages': messages, 'temperature': 0,
                'thinking': {'type': 'disabled'}, 'stream': stream, 'max_tokens': 2048}
        if structured:
            body['response_format'] = {'type': 'json_object'}
        if stream:
            body['stream_options'] = {'include_usage': True}
        return body

    async def json(self, system, payload, *, max_tokens=2048):
        if type(max_tokens) is not int or not 1 <= max_tokens <= 16384:
            raise ValueError('结构化输出预算需在 1–16384 token 之间')
        body = self.llm_body([
            {'role': 'system', 'content': system + '\n仅返回 JSON 对象。'},
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}], structured=True)
        body['max_tokens'] = max_tokens
        async with self.online:
            data = await self.post('/chat/completions', body, 'llm')
        try:
            choice = data['choices'][0]
            if choice['finish_reason'] != 'stop':
                raise ProviderError('结构化回答未完整结束')
            result = json.loads(choice['message']['content'])
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError('结构化回答格式无效') from None

    async def stream(self, messages) -> AsyncIterator[dict]:
        finished, saw_done = None, False
        async with self.online:
            self.count_call()
            try:
                async with self.http.stream('POST', self.s.deepseek_base_url.rstrip('/') + '/chat/completions',
                                            headers=self.headers('llm'), json=self.llm_body(messages, stream=True)) as response:
                    if response.is_error:
                        raise ProviderError(f'llm HTTP {response.status_code}')
                    async for line in response.aiter_lines():
                        if not line.startswith('data:'):
                            continue
                        payload = line[5:].strip()
                        if payload == '[DONE]':
                            saw_done = True
                            break
                        try:
                            data = json.loads(payload)
                            if data.get('error'):
                                raise ProviderError('LLM 流业务错误')
                            if data.get('usage') is not None:
                                usage = {'kind': 'llm', **data['usage']}
                                self.usage.append(usage)
                                self.ledger.append(usage)
                            for choice in data.get('choices', []):
                                text = choice.get('delta', {}).get('content')
                                if text:
                                    yield {'text': text}
                                if choice.get('finish_reason'):
                                    finished = choice['finish_reason']
                        except (ValueError, TypeError, AttributeError):
                            raise ProviderError('LLM 流格式无效') from None
            except httpx.HTTPError:
                raise ProviderError('LLM 流网络中断') from None
        if not saw_done or finished != 'stop':
            raise ProviderError(f'LLM 流未完整结束 ({finished or "missing"})')
