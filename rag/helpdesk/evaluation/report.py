import json
from pathlib import Path

def mean(values):
    values = [x for x in values if x is not None]
    return sum(values)/len(values) if values else None

def summary(results):
    metrics = {key:mean([r.get('retrieval',{}).get(key) for r in results]) for key in ('recall_at_5','full_coverage','mrr_at_5')}
    answerable = [r for r in results if r['expected_behavior']=='answer']
    scored = [r for r in answerable if 'judge' in r]
    human = [r for r in answerable if 'human' in r]
    metrics.update(total=len(results), execution_errors=sum(r['status']=='error' for r in results),
                   answerable=len(answerable), scored=len(scored),
                   automatic_answer_correctness=mean([int(r['judge'].get('correct') is True) for r in scored]) if len(scored)==len(answerable) else None,
                   official_answer_correctness=mean([int(r['human'].get('correct') is True and r['status']=='completed') for r in human]) if len(human)==len(answerable) else None,
                   human_reviewed=len(human),
                   needs_human_review=sum(r.get('judge',{}).get('needs_review',False) for r in results),
                   faithfulness=mean([r.get('faithfulness') for r in results]))
    metrics['model_calls']=sum(r.get('model_calls',0)+r.get('judge_calls',0) for r in results)
    metrics['usage_records']=[u for r in results for u in (r.get('usage') or [])+(r.get('judge_usage') or [])] or None
    for behavior in ('refuse','clarify'):
        subset = [r for r in results if r['expected_behavior']==behavior and 'judge' in r]
        metrics[behavior+'_correctness'] = mean([int(r['judge'].get('correct') is True) for r in subset])
        metrics[behavior+'_scored'] = len(subset)
    times = sorted(r['trace']['total_ms'] for r in results if r.get('trace',{}).get('total_ms') is not None)
    metrics['latency_ms'] = {'p50': times[int((len(times)-1)*.5)] if times else None, 'p95':times[int((len(times)-1)*.95)] if times else None}
    return metrics

def report(directory, results):
    root = Path(directory)
    data = summary(results)
    (root/'summary.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 失败与待复核样本','', '自动评分仅供辅助；正式正确率需完成人工审核。','']
    for r in results:
        if r['status']=='error' or r.get('judge',{}).get('correct') is not True or r.get('retrieval',{}).get('full_coverage') == 0:
            lines.extend([f"## {r['id']}", f"状态：{r['status']}", f"问题：{r['question']}", f"原因：{r.get('error') or r.get('judge',{}).get('reason','尚未评分/证据不完整')}", ''])
    (root/'failures.md').write_text('\n'.join(lines), encoding='utf-8')
    return data

def compare(left, right, output):
    a,b = Path(left),Path(right)
    ma,mb = (json.loads((p/'manifest.json').read_text(encoding='utf-8')) for p in (a,b))
    if any(ma[k]!=mb[k] for k in ('dataset_hash','corpus_hash','mode')):
        raise ValueError('数据或运行模式不同，不能比较成绩；需在同一新语料/数据集上重新运行')
    sa,sb = (json.loads((p/'summary.json').read_text(encoding='utf-8')) for p in (a,b))
    lines = ['# 实验对比','', '| 指标 | 基线 | 当前 | 差值 |','| --- | --- | --- | --- |']
    for k in ('recall_at_5','full_coverage','mrr_at_5','automatic_answer_correctness','execution_errors'):
        x,y = sa[k],sb[k];delta = round(y-x,4) if isinstance(x,(float,int)) and isinstance(y,(float,int)) else None
        lines.append(f'| {k} | {x} | {y} | {delta} |')
    Path(output).write_text('\n'.join(lines),encoding='utf-8')
