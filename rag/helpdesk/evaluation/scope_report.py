"""独立跨库口径：失败保留分母，问答计时与评委计时分开。"""
import html
import json
import math
from pathlib import Path

from .report import mean, report
from .dataset import sha


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def quantiles(values):
    values = sorted(values)
    return {'n': len(values), 'p50': values[max(0, math.ceil(len(values) * .5) - 1)] if values else None,
            'p95': values[max(0, math.ceil(len(values) * .95) - 1)] if values else None}


def cost_estimate(rows, prices):
    known, amount, records = 0, 0.0, [u for r in rows for u in r.get('usage', []) or []]
    for usage in records:
        rates = (prices or {}).get('rates', {}).get(usage.get('kind'), {})
        total = usage.get('total_tokens')
        prompt = usage.get('prompt_tokens', usage.get('input_tokens'))
        completion = usage.get('completion_tokens', usage.get('output_tokens'))
        if isinstance(total, (int, float)) and 'total_per_million' in rates:
            amount += total * rates['total_per_million'] / 1e6
        elif all(isinstance(v, (int, float)) for v in (prompt, completion)) and all(k in rates for k in ('input_per_million', 'output_per_million')):
            amount += (prompt * rates['input_per_million'] + completion * rates['output_per_million']) / 1e6
        else:
            continue
        known += 1
    calls = sum(r.get('model_calls', 0) for r in rows)
    complete = bool(prices) and known == len(records) and len(records) == calls
    return {'price_version': (prices or {}).get('version'), 'currency': (prices or {}).get('currency'),
            'estimated_amount': amount if complete else None, 'known_partial_amount': amount if known else None,
            'priced_records': known, 'usage_records': len(records), 'calls': calls,
            'calls_with_unknown_usage': max(0, calls - len(records)), 'complete': complete}


def scope_summary(cases, rows, prices=None):
    latest = {r['id']: r for r in rows}
    complete = len(latest) == len(cases)
    behaviors = {}
    for behavior, status in [('answer', 'completed'), ('refuse', 'refused'), ('clarify', 'clarify')]:
        subset = [latest.get(c.id) for c in cases if c.expected_behavior == behavior]
        reviewed = sum(bool(r and r.get('human')) for r in subset)
        correct = sum(bool(r and r.get('human', {}).get('correct') is True and r['status'] == status) for r in subset)
        behaviors[behavior] = {'total': len(subset), 'reviewed': reviewed, 'correct': correct,
                              'official_correctness': correct / len(subset) if subset and reviewed == len(subset) and complete else None}
    answerable = [latest.get(c.id) for c in cases if c.expected_behavior == 'answer']
    route_cases = [c for c in cases if c.expected_route_kb_ids is not None]
    route_correct = sum(latest.get(c.id, {}).get('status') not in (None, 'error') and
                        {m['kb_id'] for m in latest[c.id].get('trace', {}).get('snapshot_members', [])} == set(c.expected_route_kb_ids)
                        for c in route_cases)
    stage_means = {}
    for stage in ('candidates', 'fused', 'reranked', 'context'):
        values = [r.get('diagnosis', {}).get('stages', {}).get(stage) for r in answerable if r]
        stage_means[stage] = {'coverage': mean([v['coverage'] for v in values if v]),
                              'observed': sum(v is not None for v in values), 'answerable': len(answerable)}
    latency = {}
    for status in ('completed', 'refused', 'clarify', 'error'):
        subset = [r for r in latest.values() if r['status'] == status]
        latency[status] = {field: quantiles([r['trace'][field] for r in subset if r.get('trace', {}).get(field) is not None])
                           for field in ('server_ttft_ms', 'total_ms')}
    return {'total': len(cases), 'executed': len(latest), 'pending': len(cases) - len(latest),
            'execution_errors': sum(r['status'] == 'error' for r in latest.values()), 'behaviors': behaviors,
            'wrong_full_refusals': {'count': sum(bool(r and r['status'] == 'refused') for r in answerable),
                                    'denominator': len(answerable), 'rate': sum(bool(r and r['status'] == 'refused') for r in answerable) / len(answerable) if answerable and complete else None},
            'route': {'correct': route_correct, 'total': len(route_cases), 'accuracy': route_correct / len(route_cases) if route_cases and complete else None},
            'stages': stage_means, 'latency_ms_by_status': latency, 'cost': cost_estimate(list(latest.values()), prices),
            'qa_cost': cost_estimate([{'model_calls': r.get('qa_calls', 0), 'usage': r.get('qa_usage', [])} for r in latest.values()], prices),
            'scoring_cost': cost_estimate([{'model_calls': r.get('scoring_calls', 0), 'usage': r.get('scoring_usage', [])} for r in latest.values()], prices),
            'human_pending': sum(not latest.get(c.id, {}).get('human') for c in cases),
            'fixed_history_cases': sum(c.history_mode == 'fixed' for c in cases)}


def scope_report(directory, cases, rows, prices=None):
    report(directory, rows)
    data = scope_summary(cases, rows, prices)
    request_path = Path(directory) / 'requests.jsonl'
    if request_path.exists():
        requests = [json.loads(s) for s in request_path.read_text(encoding='utf-8').splitlines() if s.strip()]
        latest = {r['id']: r for r in rows}
        orphan = [r for r in requests if r.get('event') == 'started' and
                  (r['case_id'] not in latest or r.get('attempt') != latest[r['case_id']].get('attempt'))]
        data['issued_requests_all_attempts'] = sum(r.get('event') == 'started' for r in requests)
        data['orphan_requests'] = len(orphan)
        if orphan:
            data['cost'].update(complete=False, estimated_amount=None)
            for phase, field in [('qa', 'qa_cost'), ('judge', 'scoring_cost')]:
                if any(r.get('phase') == phase for r in orphan):
                    data[field].update(complete=False, estimated_amount=None)
    write_json(Path(directory) / 'scope-summary.json', data)
    segments = {scene: scope_summary([c for c in cases if c.scene == scene], [r for r in rows if r['id'] in {c.id for c in cases if c.scene == scene}], prices)
                for scene in sorted({c.scene for c in cases})}
    write_json(Path(directory) / 'scope-segments.json', segments)
    return data


def review_packet(cases, bundle, output, results=None):
    root = Path(output)
    root.mkdir(parents=True, exist_ok=False)
    results = results or {}
    packet = {'scope_version': bundle['scope_version'], 'cases': [c.model_dump() for c in cases],
              'results': results, 'corpus': bundle['corpus'], 'status': 'awaiting_human_review'}
    write_json(root / 'review.json', packet)
    e = lambda v: html.escape(str(v), quote=True)
    cards = []
    version_sources = {c['version_id']: c for c in bundle['corpus']['chunks']}
    def render_anchor(a):
        source = version_sources[a.source_version]
        url = source.get('source', '')
        link = f'<a href="{e(url)}" target="_blank" rel="noopener noreferrer">原始来源</a>' if url.startswith(('https://', 'http://')) else ''
        return f'<li><b>{e(source.get("title", a.source_version))} · {e(a.locator)}</b> {link}<pre>{e(a.quote)}</pre></li>'
    for c in cases:
        anchors = ''.join(render_anchor(a) for g in c.evidence_groups for a in g)
        row = results.get(c.id, {})
        cards.append(f'<details data-scene="{e(c.scene)}" data-split="{c.split}"><summary>{e(c.id)} · {c.split} · {e(c.scene)} · {c.expected_behavior}<span style="display:block;font-weight:400;font-size:14px;margin-top:6px">{e(c.question)}</span></summary>'
            f'<p>{e(c.question)}</p><p>请求范围：{e(c.scope_mode)} {e(c.kb_ids)}；预期路由：{e(c.expected_route_kb_ids)}</p>'
            f'<p>历史模式：{e(c.history_mode)}</p><pre>{e(json.dumps(c.history, ensure_ascii=False, indent=2))}</pre>'
            f'<h3>必要事实 / 参考答案</h3><pre>{e(json.dumps(c.required_facts, ensure_ascii=False, indent=2))}</pre><p>{e(c.reference_answer)}</p>'
            f'<h3>禁止事实</h3><pre>{e(json.dumps(c.forbidden_facts, ensure_ascii=False))}</pre>'
            f'<p>主题：{e(c.fact_topics or [c.theme])}；旧集重叠：{e(c.old_topic_overlap)}</p><h3>原文锚点</h3><ul>{anchors}</ul>'
            f'<h3>实际答案及阶段记录</h3><pre>{e(json.dumps(row, ensure_ascii=False, indent=2) if row else "尚未运行")}</pre></details>')
    scenes = ''.join(f'<option>{e(s)}</option>' for s in sorted({c.scene for c in cases}))
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>跨库评测人工审核</title>
<style>body{font:16px/1.6 system-ui;max-width:1100px;margin:32px auto;padding:0 24px;color:#182536;background:#f3f6fa}details{background:white;border:1px solid #ccd6e0;border-radius:10px;padding:16px;margin:12px 0}summary{cursor:pointer;font-weight:650}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7fa;padding:12px}input,select,button{padding:8px;margin-right:8px}h3{font-size:16px}</style>
<h1>跨库评测人工审核</h1><p>候选与来源审核不是实际答案裁决。本页只查看，不自动冻结或填写人工记录。</p>
<p>COUNTS。旧题来源段落重叠按逐题标注披露，题数不等于独立事实数。多轮题为固定历史重放，真实会话链另行验证。</p>
<input id="search" aria-label="搜索题目" placeholder="搜索 ID / 问题 / 事实"><select id="scene" aria-label="场景"><option value="">全部场景</option>SCENES</select>
<select id="split" aria-label="划分"><option value="">全部划分</option><option>dev</option><option>test</option></select><button id="expand">展开可见题</button><p id="count"></p>CARDS
<script>const cards=[...document.querySelectorAll('details')];function filter(){let n=0;for(const c of cards){c.hidden=!c.textContent.toLowerCase().includes(search.value.toLowerCase())||(scene.value&&c.dataset.scene!==scene.value)||(split.value&&c.dataset.split!==split.value);n+=!c.hidden}count.textContent=`显示 ${n} / ${cards.length} 题`}for(const id of ['search','scene','split'])document.getElementById(id).addEventListener('input',filter);document.getElementById('expand').onclick=()=>cards.filter(c=>!c.hidden).forEach(c=>c.open=true);filter();</script></html>'''
    counts = f'开发 {sum(c.split == "dev" for c in cases)} 题，测试 {sum(c.split == "test" for c in cases)} 题；与旧题来源段落重叠 {sum(bool(c.old_topic_overlap) for c in cases)} 题'
    (root / 'review.html').write_text(page.replace('SCENES', scenes).replace('COUNTS', counts).replace('CARDS', ''.join(cards)), encoding='utf-8')
    return {'count': len(cases), 'packet_hash': sha(packet), 'page': str((root / 'review.html').resolve())}


def paired_packet(cases, bundle, left, right, output, seed):
    paths = [Path(left), Path(right)]
    manifests = [json.loads((p / 'manifest.json').read_text(encoding='utf-8')) for p in paths]
    check_pair_configuration(*manifests)
    records = [{r['id']: r for r in [json.loads(s) for s in (p / 'cases.jsonl').read_text(encoding='utf-8').splitlines() if s.strip()]} for p in paths]
    ids = {c.id for c in cases}
    if any(set(rs) != ids for rs in records):
        raise ValueError('成对审核需同一划分全部答案')
    key, blinded = {}, {}
    for c in cases:
        order = [0, 1] if int(sha([seed, c.id]), 16) % 2 else [1, 0]
        key[c.id] = {label: manifests[i]['experiment'] for label, i in zip(('A', 'B'), order)}
        blinded[c.id] = {label: {k: records[i][c.id].get(k) for k in ('status', 'answer', 'evidence', 'citations')}
                        for label, i in zip(('A', 'B'), order)}
    packet = review_packet(cases, bundle, output, blinded)
    write_json(Path(output) / 'blind-key.json', {'seed': seed, 'mapping': key})
    return packet


def check_pair_configuration(a, b):
    fixed = ('schema', 'dataset_hash', 'scope_version', 'split', 'commit', 'prompt', 'model_routes', 'endpoints', 'context_budget', 'sampling', 'prices', 'http_budget', 'order_seed')
    if any(a.get(k) != b.get(k) for k in fixed) or a['experiment'] == b['experiment'] or a.get('cases') != b.get('cases'):
        raise ValueError('对比必须使用同一冻结配置、代码、价格及不同实验策略')


def paired_compare(left, right, output):
    left, right = Path(left), Path(right)
    a, b = [json.loads((p / 'manifest.json').read_text(encoding='utf-8')) for p in (left, right)]
    check_pair_configuration(a, b)
    records = [{r['id']: r for r in [json.loads(s) for s in (p / 'cases.jsonl').read_text(encoding='utf-8').splitlines() if s.strip()]} for p in (left, right)]
    ids = {c['id'] for c in a['cases']}
    if any(set(r) != ids for r in records) or any(not r.get('human') for rs in records for r in rs.values()):
        raise ValueError('成对对比需执行全部题并完成两组人工裁决')
    counts = dict(improved=0, regressed=0, both_correct=0, both_wrong=0)
    pairs = []
    for uid in sorted(ids):
        x, y = [r[uid] for r in records]
        correct = [r['human']['correct'] is True and r['status'] == {'answer': 'completed', 'refuse': 'refused', 'clarify': 'clarify'}[r['expected_behavior']] for r in (x, y)]
        outcome = 'both_correct' if all(correct) else 'both_wrong' if not any(correct) else 'improved' if correct[1] else 'regressed'
        counts[outcome] += 1
        pairs.append({'id': uid, 'outcome': outcome})
    data = {'counts': counts, 'net_improvement': counts['improved'] - counts['regressed'], 'pairs': pairs,
            'left': json.loads((left / 'scope-summary.json').read_text(encoding='utf-8')),
            'right': json.loads((right / 'scope-summary.json').read_text(encoding='utf-8')),
            'interpretation': '分层困难集的成对结果，不代表线上任务成功率或统计显著性'}
    write_json(output, data)
    return data
