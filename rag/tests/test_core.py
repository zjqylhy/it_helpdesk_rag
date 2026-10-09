import json
import httpx
import pytest
from helpdesk.config import Settings
from helpdesk.providers import Models, ProviderError
from helpdesk.parsing import Paragraph, chunk
from helpdesk.pipeline import rrf, valid_citations
from helpdesk.evaluation.dataset import Case, Anchor, validate, publish, save
from helpdesk.evaluation.retrieval_metrics import retrieval_metrics

def settings():
    return Settings(_env_file=None, deepseek_api_key='test',dashscope_api_key='test',dashscope_base_url='https://mock',embedding_dimension=2)

@pytest.mark.asyncio
async def test_embedding_alignment_and_dimension():
    seen=[]
    def handle(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200,json={'output':{'embeddings':[{'text_index':1,'embedding':[0,1]},{'text_index':0,'embedding':[1,0]}]}})
    m=Models(settings(),httpx.MockTransport(handle))
    assert await m.embed(['a','b'],'query') == [[1,0],[0,1]]
    assert seen[0]['parameters']['text_type']=='query'
    with pytest.raises(ProviderError):await m.embed(['a'])
    await m.close()

@pytest.mark.asyncio
async def test_duplicate_rerank_rejected():
    m=Models(settings(),httpx.MockTransport(lambda r:httpx.Response(200,json={'output':{'results':[{'index':0,'relevance_score':.8},{'index':0,'relevance_score':.6}]}})))
    with pytest.raises(ProviderError):await m.rerank('q',['a','b'])
    await m.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('finish,done,valid',[('stop',True,True),('length',True,False),('stop',False,False)])
async def test_stream_terminal(finish,done,valid):
    payload='data: '+json.dumps({'choices':[{'delta':{'reasoning_content':'secret'},'finish_reason':None}]})+'\n\n'
    payload+='data: '+json.dumps({'choices':[{'delta':{'content':'回答'},'finish_reason':finish}],'usage':{'total_tokens':8}})+'\n\n'
    if done:payload+='data: [DONE]\n\n'
    m=Models(settings(),httpx.MockTransport(lambda r:httpx.Response(200,text=payload)))
    if valid:
        assert [x async for x in m.stream([])]==[{'text':'回答'}]
        assert m.usage[0]['total_tokens']==8
    else:
        with pytest.raises(ProviderError):[x async for x in m.stream([])]
    await m.close()

@pytest.mark.asyncio
async def test_call_budget_includes_attempts():
    m=Models(settings(),httpx.MockTransport(lambda r:httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]})))
    m.max_calls=1
    await m.json('test',{})
    with pytest.raises(ProviderError):await m.json('test',{})
    await m.close()

def test_rrf_and_citations():
    assert rrf([['a','b'],['b','c']])[0]=='b'
    with pytest.raises(ProviderError):valid_citations('事实 [C0]',[{'id':'x'}])
    with pytest.raises(ProviderError):valid_citations('无引用',[{'id':'x'}])
    with pytest.raises(ProviderError):valid_citations('非标准编号 [C01]',[{'id':'x'}])
    with pytest.raises(ProviderError):valid_citations('混入非法编号 [Cfoo] [C1]',[{'id':'x'}])
    assert valid_citations('事实 [C1] [C1]',[{'id':'x'}])==[{'id':'x','label':'C1'}]

def data():
    text='abcdefghij'
    corpus={'knowledge_base':'k','corpus_version':'s','paragraphs':[{'version_id':'v','locator':{'paragraph':1},'text':text}]}
    case=Case(id='q',split='test',theme='vpn',knowledge_base='k',corpus_version='s',question='q',expected_behavior='answer',required_facts=['f'],evidence_groups=[[Anchor(source_version='v',locator={'paragraph':1},quote='cdefgh')]])
    chunks=[{**c,'version_id':'v'} for c in chunk([Paragraph(text,{'paragraph':1})],size=6,overlap=2)]
    return corpus,case,chunks

def test_anchor_cross_chunk_and_duplicate():
    corpus,case,chunks=data()
    assert retrieval_metrics(case,[chunks[0]],corpus)['recall_at_5']==0
    assert retrieval_metrics(case,[chunks[0],chunks[0]],corpus)['recall_at_5']==0
    metrics=retrieval_metrics(case,chunks,corpus)
    assert metrics['recall_at_5']==1 and metrics['mrr_at_5']==.5

def test_publish_requires_review_and_never_overwrites(tmp_path):
    corpus,case,_=data();draft=tmp_path/'draft.jsonl';save(draft,[case])
    with pytest.raises(ValueError):publish(draft,corpus,tmp_path/'releases','v1')
    case.status='active';case.review_record={'reviewer':'human','approved_at':'today'};save(draft,[case])
    publish(draft,corpus,tmp_path/'releases','v1')
    with pytest.raises(FileExistsError):publish(draft,corpus,tmp_path/'releases','v1')
    changed={**corpus,'corpus_version':'s2'}
    with pytest.raises(ValueError):validate([case],changed)
