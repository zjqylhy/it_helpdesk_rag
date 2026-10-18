"""真实跨库入口重放；模型请求先记账，实际答案始终待人工裁决。"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from ..pipeline import Pipeline, TRACE_VERSION
from .dataset import sha
from .scope_dataset import SCHEMA, load_scope, validate_scope
from .scope_store import FrozenScopeStore
from .diagnostics import diagnose
from .retrieval_metrics import retrieval_metrics
from .runner import git_commit
from .answer_judge import judge
from .scope_report import scope_report, write_json


async def run_scope(settings, models, dataset_path, bundle_path, output, *, split='dev', experiment='baseline',
                    resume=False, judge_answers=False, prices=None, case_ids=None, order_seed=0, http_budget=None):
    if experiment not in ('baseline', 'split-v1'):
        raise ValueError('未知查询实验策略')
    all_cases = load_scope(dataset_path)
    bundle = json.loads(Path(bundle_path).read_text(encoding='utf-8-sig'))
    validate_scope(all_cases, bundle, published=True)
    cases = [c for c in all_cases if c.split == split]
    if not cases:
        raise ValueError('所选划分无题目')
    if case_ids is not None and not set(case_ids) <= {c.id for c in cases}:
        raise ValueError('执行 ID 超出当前划分')
    store = FrozenScopeStore(settings, bundle)
    await store.verify()
    manifest = {'schema': SCHEMA, 'dataset_hash': sha([c.model_dump() for c in cases]), 'scope_version': bundle['scope_version'],
                'split': split, 'commit': git_commit(), 'prompt': 'qa-v3-scope', 'experiment': experiment,
                'model_routes': settings.model_routes(), 'context_budget': settings.context_budget,
                'sampling': {'temperature': 0, 'max_tokens': 2048}, 'judge': judge_answers, 'prices': prices,
                'endpoints': {kind: settings.model_gateway_base_url if settings.uses_gateway(kind) else settings.deepseek_base_url if kind == 'llm' else settings.dashscope_base_url for kind in ('llm', 'embedding', 'rerank')},
                'order_seed': order_seed, 'http_budget': http_budget if http_budget is not None else getattr(models, 'max_calls', None),
                'trace_version': TRACE_VERSION,
                'cases': [{'id': c.id, 'expected_behavior': c.expected_behavior} for c in cases]}
    root = Path(output)
    rows, requests = [], []
    if root.exists():
        if not resume or json.loads((root / 'manifest.json').read_text(encoding='utf-8')) != manifest:
            raise ValueError('续跑清单不一致或未指定 --resume')
        if (root / 'cases.jsonl').exists():
            rows = [json.loads(s) for s in (root / 'cases.jsonl').read_text(encoding='utf-8').splitlines() if s.strip()]
        if (root / 'requests.jsonl').exists():
            requests = [json.loads(s) for s in (root / 'requests.jsonl').read_text(encoding='utf-8').splitlines() if s.strip()]
    else:
        root.mkdir(parents=True)
        write_json(root / 'manifest.json', manifest)
    latest = {r['id']: r for r in rows}
    previous_observer = getattr(models, 'call_observer', None)
    current = {}

    def observe(event):
        record = {**event, **current, 'at': datetime.now(timezone.utc).isoformat()}
        with (root / 'requests.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
            f.flush()
        requests.append(record)

    models.call_observer = observe
    try:
        for c in sorted(cases, key=lambda c: sha([order_seed, c.id])):
            # 所有已落盘终态保留，故障恢复另开运行，不用续跑挑较好答案。
            if c.id in latest:
                continue
            if case_ids is not None and c.id not in case_ids:
                continue
            if getattr(models, 'max_calls', None) is not None and models.calls >= models.max_calls:
                break
            current.update(case_id=c.id, phase='qa', attempt=1 + sum(r.get('case_id') == c.id and r.get('event') == 'attempt' for r in requests))
            observe({'event': 'attempt'})
            start, call_start, ledger_start = time.perf_counter(), models.calls, len(models.ledger)
            row = {'id': c.id, 'question': c.question, 'scene': c.scene, 'expected_behavior': c.expected_behavior,
                   'status': 'error', 'answer': '', 'evidence': [], 'citations': [], 'trace': {}}
            pipeline = Pipeline(store, models, experiment=experiment)
            try:
                async for event, data in pipeline.events(None, c.question, c.history, kb_ids=c.kb_ids):
                    await store.check()
                    if event == 'answer_delta':
                        row['answer'] += data['text']
                    elif event == 'refusal':
                        row['answer'] = data['text']
                    elif event == 'evidence':
                        row.update(evidence=data['evidence'], decision=data['decision'])
                    elif event == 'citations':
                        row['citations'] = data['citations']
                    elif event == 'done':
                        row.update(status=data['status'], trace=data['trace'])
                await store.check()
            except Exception as exc:
                row.update(status='error', error=type(exc).__name__, trace=getattr(pipeline, 'last_trace', {}))
                if not row['evidence']:
                    row['evidence'] = getattr(pipeline, 'last_evidence', [])
                row['trace']['total_ms'] = round((time.perf_counter() - start) * 1000)
            row['retrieval'] = retrieval_metrics(c, row['evidence'], bundle['corpus'])
            row['diagnosis'] = diagnose(c, row, bundle['corpus'])
            row['qa_calls'] = models.calls - call_start
            row['qa_usage'] = models.ledger[ledger_start:]
            if judge_answers:
                current['phase'] = 'judge'
                try:
                    row['judge'] = await judge(models, c, row)
                except Exception as exc:
                    row['judge'] = {'correct': False, 'needs_review': True, 'error': type(exc).__name__}
            row.update(model_calls=models.calls - call_start, usage=models.ledger[ledger_start:] or [],
                       scoring_calls=models.calls - call_start - row['qa_calls'], scoring_usage=models.ledger[ledger_start + len(row['qa_usage']):],
                       attempt=current['attempt'])
            with (root / 'cases.jsonl').open('a', encoding='utf-8') as f:
                f.write(json.dumps(row, ensure_ascii=False) + '\n')
            latest[c.id] = row
            scope_report(root, cases, list(latest.values()), prices)
    finally:
        models.call_observer = previous_observer
        data = scope_report(root, cases, list(latest.values()), prices)
        data['issued_requests_all_attempts'] = sum(r.get('event') == 'started' for r in requests)
        data['orphan_requests'] = sum(r.get('event') == 'started' and (r['case_id'] not in latest or r.get('attempt') != latest[r['case_id']].get('attempt')) for r in requests)
        if data['orphan_requests']:
            data['cost'].update(complete=False, estimated_amount=None)
        write_json(root / 'scope-summary.json', data)
    return data


async def run_pair_scope(settings, models, dataset_path, bundle_path, output, *, split, seed, max_calls, prices=None, judge_answers=False, resume=False):
    """逐题交错执行，整个实验共享持久化的 HTTP 请求上限。"""
    cases = [c for c in load_scope(dataset_path) if c.split == split]
    if not cases or max_calls < 1:
        raise ValueError('实验需非空划分和正整数请求上限')
    bundle = json.loads(Path(bundle_path).read_text(encoding='utf-8-sig'))
    plan = {'dataset_hash': sha([c.model_dump() for c in cases]), 'scope_version': bundle['scope_version'],
            'split': split, 'seed': seed, 'max_calls': max_calls, 'commit': git_commit(), 'prices': prices, 'judge': judge_answers}
    root = Path(output)
    if root.exists():
        if not resume or json.loads((root / 'plan.json').read_text(encoding='utf-8')) != plan:
            raise ValueError('成对实验续跑计划不一致')
    else:
        root.mkdir(parents=True); write_json(root / 'plan.json', plan)
    spent = 0
    for strategy in ('baseline', 'split-v1'):
        path = root / strategy / 'requests.jsonl'
        if path.exists():
            spent += sum(json.loads(s).get('event') == 'started' for s in path.read_text(encoding='utf-8').splitlines() if s.strip())
    models.max_calls = models.calls + max(0, max_calls - spent)
    for strategy in ('baseline', 'split-v1'):
        target = root / strategy
        await run_scope(settings, models, dataset_path, bundle_path, target, split=split, experiment=strategy,
                        resume=target.exists(), judge_answers=judge_answers, prices=prices,
                        case_ids=[], order_seed=seed, http_budget=max_calls)
    for c in sorted(cases, key=lambda c: sha([seed, c.id])):
        order = ['baseline', 'split-v1'] if int(sha([seed, c.id, 'order']), 16) % 2 else ['split-v1', 'baseline']
        for strategy in order:
            if models.calls >= models.max_calls:
                break
            target = root / strategy
            await run_scope(settings, models, dataset_path, bundle_path, target, split=split, experiment=strategy,
                            resume=target.exists(), judge_answers=judge_answers, prices=prices,
                            case_ids=[c.id], order_seed=seed, http_budget=max_calls)
    write_json(root / 'execution.json', {'seed': seed, 'http_calls_previous': spent, 'http_calls_this_process': models.calls,
               'max_calls': max_calls, 'order': [c.id for c in sorted(cases, key=lambda c: sha([seed, c.id]))]})
    return {'baseline': json.loads((root / 'baseline/scope-summary.json').read_text(encoding='utf-8')),
            'split-v1': json.loads((root / 'split-v1/scope-summary.json').read_text(encoding='utf-8'))}
