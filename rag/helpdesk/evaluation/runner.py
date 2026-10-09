import json
import subprocess
import time
from pathlib import Path
from .dataset import load, validate, sha
from .retrieval_metrics import retrieval_metrics
from .answer_judge import judge
from .report import report
from ..pipeline import Pipeline, PROMPT_VERSION

def git_commit():
    try:
        return subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip()
    except (OSError,subprocess.CalledProcessError):
        return 'unknown'

async def run(settings, store, models, dataset_path, corpus_path, output, variant='D', mode='retrieval', resume=False, split='dev', queries=None, judge_answers=False, ragas=False):
    cases = [c for c in load(dataset_path) if c.split == split and c.status != 'retired']
    if not cases:
        raise ValueError('所选划分无样本')
    corpus = json.loads(Path(corpus_path).read_text(encoding='utf-8'))
    validate(cases, corpus, published=True)
    actual = await store.export_snapshot(corpus['knowledge_base'],corpus['corpus_version'])
    if sha(actual) != sha(corpus):
        raise ValueError('数据库快照与冻结语料导出不一致')
    manifest = {'dataset_hash':sha([c.model_dump() for c in cases]), 'corpus_hash':sha(corpus),
                'corpus_version':corpus['corpus_version'],'variant':variant,'mode':mode,'split':split,
                'commit':git_commit(),'prompt':PROMPT_VERSION,'llm':settings.llm_model,
                'embedding':settings.embedding_model,'dimension':settings.embedding_dimension,
                'rerank':settings.rerank_model,'context_budget':settings.context_budget,
                'queries_hash':sha(queries) if queries is not None else None, 'judge':judge_answers,'ragas':ragas}
    root = Path(output)
    results = []
    if root.exists():
        if not resume:
            raise ValueError('输出目录已存在；续跑需 --resume 且清单完全相同')
        previous = json.loads((root/'manifest.json').read_text(encoding='utf-8'))
        if manifest != previous:
            raise ValueError('续跑清单不一致')
        if (root/'cases.jsonl').exists():
            results = [json.loads(line) for line in (root/'cases.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    else:
        root.mkdir(parents=True)
        (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    completed = {r['id'] for r in results if r['status'] != 'error'}
    # 失败样本重跑时替换最终汇总记录，旧文件中的执行历史仍保留。
    latest = {r['id']:r for r in results}
    pipeline = Pipeline(store, models)
    for case in cases:
        if case.id in completed:
            continue
        started = time.perf_counter()
        call_start, usage_start = models.calls, len(models.ledger)
        result = {'id':case.id,'case_revision':case.case_revision,'question':case.question,'expected_behavior':case.expected_behavior,
                  'answer':'','evidence':[],'citations':[],'status':'error','trace':{}}
        try:
            if mode == 'retrieval':
                query = queries[case.id] if queries else await pipeline.rewrite(case.question, case.history)
                snapshot = await store.snapshot(case.knowledge_base,case.corpus_version)
                evidence, ranking = await pipeline.retrieve(snapshot,query,variant)
                result.update(status='retrieved',evidence=evidence,trace={'ranking':ranking,'query':query,'total_ms':round((time.perf_counter()-started)*1000)})
            else:
                async for event,data in pipeline.events(case.knowledge_base,case.question,case.history,case.corpus_version,variant):
                    if event == 'answer_delta': result['answer'] += data['text']
                    if event == 'refusal': result['answer'] = data['text']
                    if event == 'evidence': result['evidence'] = data['evidence']; result['decision'] = data['decision']
                    if event == 'citations': result['citations'] = data['citations']
                    if event == 'done': result.update(status=data['status'],trace=data['trace'])
            result['retrieval'] = retrieval_metrics(case,result['evidence'],corpus)
            if judge_answers and mode != 'retrieval':
                try:
                    result['judge'] = await judge(models,case,result)
                except Exception as exc:
                    result['judge'] = {'correct':False,'needs_review':True,'error':type(exc).__name__}
            if ragas and result['status']=='completed':
                from .ragas_adapter import faithfulness
                try:
                    result['faithfulness'] = await faithfulness(models,result)
                except Exception as exc:
                    result['faithfulness_error'] = type(exc).__name__
        except Exception as exc:
            result['error'] = str(exc) if isinstance(exc,ValueError) or type(exc).__name__=='ProviderError' else type(exc).__name__
            if case.expected_behavior == 'answer':
                result['retrieval'] = {'recall_at_5':0,'full_coverage':0,'mrr_at_5':0}
                if judge_answers:
                    result['judge'] = {'correct':False,'needs_review':True,'reason':'执行失败'}
        latest[case.id] = result
        result['model_calls'] = models.calls-call_start
        result['usage'] = models.ledger[usage_start:] or None
        with (root/'cases.jsonl').open('a',encoding='utf-8') as f:
            f.write(json.dumps(result,ensure_ascii=False)+'\n')
        report(root,list(latest.values()))
    return report(root,list(latest.values()))
