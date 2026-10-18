"""网关契约：别名与扁平参数、完整输出校验、失败不回退。"""
import json
import httpx
import pytest
from helpdesk.config import Settings
from helpdesk.providers import Models, ProviderError


def settings(**changes):
    values = dict(_env_file=None, chat_gateway_enabled=True, retrieval_gateway_enabled=True,
                  gateway_api_key='test-application-key', model_gateway_base_url='https://localhost:9443/v1',
                  deepseek_api_key='test-vendor-key', dashscope_api_key='test-vendor-key')
    values.update(changes)
    return Settings(**values)


@pytest.mark.asyncio
async def test_gateway_embedding_batch_alignment_and_model_identity():
    requests = []
    def handle(request):
        body = json.loads(request.content)
        requests.append((request, body))
        rows = [{'index': i, 'embedding': [i+1.0] + [0.0]*1023} for i in reversed(range(len(body['input'])))]
        return httpx.Response(200, json={'data': rows, 'usage': {'total_tokens': 3}})
    with_gateway = Models(settings(), httpx.MockTransport(handle))
    try:
        vectors = await with_gateway.embed([str(i) for i in range(21)], 'query')
        assert len(vectors) == 21 and vectors[0][0] == 1 and vectors[19][0] == 20
        assert [len(body['input']) for _,body in requests] == [20,1]
        for request, body in requests:
            assert str(request.url) == 'https://localhost:9443/v1/embeddings'
            assert request.headers['Authorization'] == 'Bearer test-application-key'
            assert set(body) == {'model','input','dimensions','encoding_format'}
            assert body['model'] == 'embedding' and body['dimensions'] == 1024 and body['encoding_format'] == 'float'
        assert with_gateway.s.embedding_model == 'qwen3.7-text-embedding'
    finally:
        await with_gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('rows', [None, [None], [{'index':0,'embedding':[1,0]}],
    [{'index':0,'embedding':[0]*1024}], [{'index':True,'embedding':[1]*1024}],
    [{'index':0,'embedding':[float('inf')]*1024}],
    [{'index':0,'embedding':[1]*1024},{'index':0,'embedding':[1]*1024}]])
async def test_gateway_rejects_invalid_vectors(rows):
    # 非有限值由已解析响应注入，避免 JSON 编码器先拒绝它。
    model = Models(settings(), httpx.MockTransport(lambda _: httpx.Response(200, text='{}')))
    async def post(*args): return {'data':rows}
    model.post = post
    try:
        with pytest.raises(ProviderError): await model.embed(['a'])
    finally:
        await model.close()


@pytest.mark.asyncio
async def test_gateway_rerank_flat_payload_and_order():
    def handle(request):
        assert request.url.path == '/v1/rerank'
        assert request.headers['Authorization'] == 'Bearer test-application-key'
        assert json.loads(request.content) == {'model':'rerank','query':'q','documents':['a','b'],
                                              'top_n':2,'return_documents':False}
        return httpx.Response(200,json={'results':[{'index':0,'relevance_score':.2},{'index':1,'relevance_score':.8}]})
    model = Models(settings(), httpx.MockTransport(handle))
    try:
        assert [row['index'] for row in await model.rerank('q',['a','b'])] == [1,0]
    finally:
        await model.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('rows', [[{'index':0,'relevance_score':.8},{'index':0,'relevance_score':.5}],
    [{'index':2,'relevance_score':.8}], [{'index':True,'relevance_score':.8}],
    [{'index':0,'relevance_score':float('nan')}], [{'index':0,'relevance_score':.8}], None])
async def test_gateway_rejects_invalid_rerank(rows):
    model = Models(settings(), httpx.MockTransport(lambda _: httpx.Response(200,text='{}')))
    async def post(*args): return {'results':rows}
    model.post = post
    try:
        with pytest.raises(ProviderError): await model.rerank('q',['a','b'])
    finally:
        await model.close()


@pytest.mark.asyncio
async def test_gateway_chat_json_stream_and_tls_host():
    def handle(request):
        assert request.url.host == 'host.docker.internal'
        assert request.headers['Host'] == 'localhost:9443'
        assert request.extensions['sni_hostname'] == 'localhost'
        assert request.url.path == '/v1/chat/completions'
        assert request.headers['Authorization'] == 'Bearer test-application-key'
        body = json.loads(request.content)
        assert body['model'] == 'chat' and 'thinking' not in body
        if not body['stream']:
            assert body['response_format'] == {'type':'json_object'}
            assert body['max_tokens'] == 64
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]})
        assert body['stream_options'] == {'include_usage':True}
        return httpx.Response(200,text='data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}],"usage":{"total_tokens":2}}\n\ndata: [DONE]\n\n')
    model = Models(settings(model_gateway_connect_host='host.docker.internal'), httpx.MockTransport(handle))
    try:
        assert await model.json('test',{},max_tokens=64) == {'ok':True}
        assert [event async for event in model.stream([])] == [{'text':'ok'}]
        assert model.usage[-1]['total_tokens'] == 2
    finally:
        await model.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status',[401,302,500])
async def test_gateway_failure_never_falls_back(status):
    requests=[]
    def handle(request):
        requests.append(request)
        return httpx.Response(status,headers={'Location':'https://vendor.invalid'})
    model=Models(settings(),httpx.MockTransport(handle))
    try:
        with pytest.raises(ProviderError): await model.embed(['a'])
        assert len(requests)==1 and requests[0].url.host=='localhost'
    finally:
        await model.close()


@pytest.mark.asyncio
async def test_gateway_missing_key_does_not_call_vendor():
    model=Models(settings(gateway_api_key=''),httpx.MockTransport(lambda _:pytest.fail('不应发送请求')))
    try:
        with pytest.raises(ProviderError,match='API Key'): await model.embed(['a'])
    finally:
        await model.close()


def test_routes_distinguish_aliases_and_configured_vendor_models():
    config=settings()
    assert config.model_routes()['embedding']=={'provider':'gateway','model':'embedding'}
    assert config.embedding_model=='qwen3.7-text-embedding'
    assert 'test-application-key' not in repr(config)
