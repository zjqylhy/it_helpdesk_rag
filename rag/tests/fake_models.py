"""接口契约替身：仅验证工程链路，其回答不作为模型效果评测。"""
import asyncio
import json
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app=FastAPI()

@app.post('/api/v1/services/embeddings/text-embedding/text-embedding')
async def embedding(request: Request):
    body=await request.json()
    return {'output':{'embeddings':[{'text_index':i,'embedding':[1.0,0.5] + [0.0]*(body['parameters']['dimension']-2)} for i in range(len(body['input']['texts']))]},'usage':{'total_tokens':12}}

@app.post('/api/v1/services/rerank/text-rerank/text-rerank')
async def rerank(request: Request):
    body=await request.json()
    return {'output':{'results':[{'index':i,'relevance_score':1/(i+1)} for i in range(body['parameters']['top_n'])]},'usage':{'total_tokens':10}}

@app.post('/chat/completions')
async def completion(request: Request):
    body=await request.json();system=body['messages'][0]['content'];payload=json.loads(body['messages'][-1]['content'])
    if not body.get('stream'):
        if 'available_knowledge_bases' in payload:
            available = payload['available_knowledge_bases']
            question = payload['question']
            vpn = [b for b in available if 'VPN' in b['name']]
            selected = [b for b in available if b['organization'] in question]
            result = {'query': question, 'behavior': 'search', 'kb_ids': [b['id'] for b in selected], 'requires_all': False}
            if '比较' in question or '区别' in question:
                result.update(kb_ids=[b['id'] for b in (selected or vpn)], requires_all=True)
            elif 'VPN' in question.upper() and not selected:
                if len(vpn) > 1:
                    result.update(behavior='clarify', message='请问你属于哪个学校？', option_ids=[b['id'] for b in vpn])
                elif len(vpn) == 1:
                    result['kb_ids'] = [vpn[0]['id']]
            elif selected and payload['history'] and len(question) < 15:
                previous = next((h['content'] for h in reversed(payload['history']) if h['role'] == 'user'), '')
                result['query'] = selected[0]['organization'] + previous
        elif '消解指代' in system:result={'query':payload['question']}
        elif '必要事实' in system and '独立评委' in system:result={'facts':[{'fact':f,'pass':True,'reason':'契约测试'} for f in payload['required_facts']],'has_error':False,'behavior_reason_valid':True,'citation_support':True,'reason':'仅用于工程测试'}
        else:result={'behavior':'refuse' if '天气' in payload.get('question','') else 'answer','reason':'依据测试资料','evidence_labels':['C1']}
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result,ensure_ascii=False)}}],'usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}}
    async def stream():
        text='测试环境：请先打开客户端，然后输入组织提供的服务器地址。[C1]'
        if payload.get('evidence') and 'knowledge_base' in payload['evidence'][0]:
            seen = set()
            answers = []
            for evidence in payload['evidence']:
                if evidence['organization'] not in seen:
                    seen.add(evidence['organization'])
                    answers.append(f"{evidence['organization']}：{evidence['text']} [{evidence['label']}]。")
            text = '\n'.join(answers)
        if '慢速' in payload.get('question',''):await asyncio.sleep(2)
        for offset in range(0,len(text),5):
            await asyncio.sleep(.04)
            yield 'data: '+json.dumps({'choices':[{'delta':{'content':text[offset:offset+5]},'finish_reason':None}]},ensure_ascii=False)+'\n\n'
        yield 'data: '+json.dumps({'choices':[{'delta':{},'finish_reason':'stop'}],'usage':{'prompt_tokens':20,'completion_tokens':20,'total_tokens':40}})+'\n\n'
        yield 'data: [DONE]\n\n'
    return StreamingResponse(stream(),media_type='text/event-stream')
