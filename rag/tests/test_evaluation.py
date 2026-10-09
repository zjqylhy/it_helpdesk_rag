import json
import os
import httpx
import pytest
from helpdesk.config import Settings
from helpdesk.providers import Models
from helpdesk.evaluation.dataset import Case, save, load
from helpdesk.evaluation.dataset_maintenance import affected
from helpdesk.evaluation.report import summary, compare, report

def test_theme_leakage_rejected(tmp_path):
    base=dict(id='a',theme='one-fact',knowledge_base='k',corpus_version='s',question='q',expected_behavior='refuse')
    p=tmp_path/'cases.jsonl'
    save(p,[Case(**base,split='dev'),Case(**{**base,'id':'b'},split='test')])
    with pytest.raises(ValueError):load(p)

def test_document_change_also_checks_negatives():
    c=Case(id='negative',split='test',theme='no-answer',knowledge_base='k',corpus_version='s',question='q',expected_behavior='refuse')
    rows=affected([c],{'paragraphs':[]},{'paragraphs':[{'version_id':'new'}]})
    assert rows[0]['id']=='negative'

def test_failure_denominator_and_pending_review():
    rows=[{'id':'a','status':'completed','expected_behavior':'answer','judge':{'correct':True,'needs_review':True}},
          {'id':'b','status':'error','expected_behavior':'answer','judge':{'correct':False,'needs_review':True}}]
    result=summary(rows)
    assert result['automatic_answer_correctness']==.5
    assert result['official_answer_correctness'] is None
    assert summary([])['automatic_answer_correctness'] is None
    del rows[1]['judge']
    assert summary(rows)['automatic_answer_correctness'] is None

def test_comparison_refuses_different_corpus(tmp_path):
    a,b=tmp_path/'a',tmp_path/'b';a.mkdir();b.mkdir()
    for root,hashvalue in [(a,'old'),(b,'new')]:
        (root/'manifest.json').write_text(json.dumps({'dataset_hash':'d','corpus_hash':hashvalue,'mode':'retrieval'}))
    with pytest.raises(ValueError):compare(a,b,tmp_path/'comparison.md')

def test_incomplete_run_remains_pending(tmp_path):
    (tmp_path/'manifest.json').write_text(json.dumps({'cases':[{'id':'a','expected_behavior':'answer'},{'id':'b','expected_behavior':'answer'}]}))
    data=report(tmp_path,[{'id':'a','question':'q','status':'completed','expected_behavior':'answer','judge':{'correct':True}}])
    assert data['total']==2 and data['pending']==1
    assert data['automatic_answer_correctness'] is None

@pytest.mark.asyncio
async def test_ragas_uses_configured_model_and_budget():
    os.environ['RAGAS_DO_NOT_TRACK']='true'
    from helpdesk.evaluation.ragas_adapter import faithfulness
    def handle(request):
        body=json.loads(request.content)
        task=json.loads(body['messages'][-1]['content'])['task']
        result={'statements':['测试事实']} if 'break down' in task.lower() else {'statements':[{'statement':'测试事实','reason':'有证据','verdict':1}]}
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result)}}],'usage':{'total_tokens':10}})
    models=Models(Settings(_env_file=None,deepseek_api_key='contract'),httpx.MockTransport(handle));models.max_calls=4
    score=await faithfulness(models,{'question':'q','answer':'测试事实','evidence':[{'text':'测试事实'}]})
    assert score==1
    assert models.calls==2 and len(models.ledger)==2
    await models.close()
