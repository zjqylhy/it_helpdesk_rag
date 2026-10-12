import asyncio
import json
import math
import ssl
from collections.abc import AsyncIterator
import httpx
from .config import Settings

class ProviderError(RuntimeError):
    pass

class Models:
    def __init__(self, settings: Settings, transport=None):
        self.s = settings
        self.http = httpx.AsyncClient(timeout=settings.model_timeout, transport=transport)
        self.gateway_http = None
        if settings.chat_gateway_enabled or settings.retrieval_gateway_enabled:
            context = ssl.create_default_context()
            if settings.model_gateway_ca_file:
                context.load_verify_locations(cafile=settings.model_gateway_ca_file)
            self.gateway_http = httpx.AsyncClient(timeout=settings.model_timeout, transport=transport,
                                                 verify=context, trust_env=False, follow_redirects=False)
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
        if self.gateway_http:
            await self.gateway_http.aclose()

    def headers(self, kind):
        key = self.s.gateway_api_key if self.s.uses_gateway(kind) else self.s.deepseek_api_key if kind == 'llm' else self.s.dashscope_api_key
        if not key:
            raise ProviderError(f'{kind} API Key 未配置')
        return {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}

    def request_target(self, path, kind):
        gateway = self.s.uses_gateway(kind)
        base = self.s.model_gateway_base_url if gateway else self.s.deepseek_base_url if kind == 'llm' else self.s.dashscope_base_url
        url = httpx.URL(base.rstrip('/') + path)
        if (url.scheme != 'https' and not (url.scheme == 'http' and url.host in ('127.0.0.1', 'localhost'))
                or url.username or url.password or url.query or url.fragment):
            raise ProviderError('模型服务地址需配置 HTTPS 或本机 HTTP')
        headers, extensions = self.headers(kind), {}
        if gateway:
            if url.scheme != 'https' or httpx.URL(base).path.rstrip('/') != '/v1':
                raise ProviderError('网关地址需为带 /v1 的 HTTPS 地址')
            if self.s.model_gateway_connect_host:
                headers['Host'] = url.netloc.decode('ascii')
                extensions['sni_hostname'] = url.host
                url = url.copy_with(host=self.s.model_gateway_connect_host)
        return self.gateway_http if gateway else self.http, url, headers, extensions

    async def post(self, path, body, kind):
        client, url, headers, extensions = self.request_target(path, kind)
        for attempt in range(3):
            self.count_call()
            try:
                response = await client.post(url, json=body, headers=headers, extensions=extensions)
            except httpx.HTTPError:
                raise ProviderError(f'{kind} 网络请求失败') from None
            if response.status_code in (429, 502, 503, 504) and attempt < 2:
                retry = response.headers.get('Retry-After', '')
                delay = min(float(retry), 10) if retry.replace('.', '', 1).isdigit() else 2 ** attempt
                await asyncio.sleep(delay)
                continue
            if not response.is_success:
                # 提供商原始错误可能包含请求内容，公共响应只输出分类和状态码。
                raise ProviderError(f'{kind} HTTP {response.status_code}')
            try:
                data = response.json()
            except ValueError:
                raise ProviderError(f'{kind} 返回非 JSON') from None
            if not isinstance(data, dict):
                raise ProviderError(f'{kind} 返回格式无效')
            if data.get('code') or data.get('error'):
                raise ProviderError(f'{kind} 业务错误')
            if isinstance(data.get('usage'), dict):
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
                if self.s.retrieval_gateway_enabled:
                    data = await self.post('/embeddings', {
                        'model': self.s.gateway_embedding_model, 'input': batch,
                        'dimensions': self.s.embedding_dimension, 'encoding_format': 'float'}, 'embedding')
                    rows = data.get('data', [])
                    index_key = 'index'
                else:
                    data = await self.post('/api/v1/services/embeddings/text-embedding/text-embedding', {
                        'model': self.s.embedding_model, 'input': {'texts': batch},
                        'parameters': {'text_type': text_type, 'dimension': self.s.embedding_dimension, 'output_type': 'dense'}}, 'embedding')
                    rows = data.get('output', {}).get('embeddings', [])
                    index_key = 'text_index'
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise ProviderError('Embedding 返回格式无效')
                aligned = {}
                for row in rows:
                    index, vector = row.get(index_key), row.get('embedding')
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
        if self.s.retrieval_gateway_enabled:
            data = await self.post('/rerank', {'model': self.s.gateway_rerank_model, 'query': query,
                'documents': documents, 'top_n': min(top_n, len(documents)), 'return_documents': False}, 'rerank')
            rows = data.get('results', [])
        else:
            data = await self.post('/api/v1/services/rerank/text-rerank/text-rerank', {
                'model': self.s.rerank_model, 'input': {'query': query, 'documents': documents},
                'parameters': {'top_n': min(top_n, len(documents))}}, 'rerank')
            rows = data.get('output', {}).get('results', [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ProviderError('重排返回格式无效')
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
        body = {'model': self.s.gateway_chat_model if self.s.chat_gateway_enabled else self.s.llm_model,
                'messages': messages, 'temperature': 0, 'stream': stream, 'max_tokens': 2048}
        if not self.s.chat_gateway_enabled:
            body['thinking'] = {'type': 'disabled'}
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
        client, url, headers, extensions = self.request_target('/chat/completions', 'llm')
        async with self.online:
            self.count_call()
            try:
                async with client.stream('POST', url, headers=headers, extensions=extensions,
                                         json=self.llm_body(messages, stream=True)) as response:
                    if not response.is_success:
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
