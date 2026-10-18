"""锚点覆盖用于定位证据损失，不将其自动当作答案的语义裁决。"""
from .retrieval_metrics import covered


def diagnose(case, row, corpus):
    if case.expected_behavior != 'answer':
        return {'stages': {}, 'primary': 'behavior' if row.get('human', {}).get('correct') is False else 'not_applicable',
                'confidence': 'needs_review'}
    chunks = {c['id']: c for c in corpus['chunks']}
    for evidence in row.get('evidence', []):
        frozen = chunks.get(evidence['id'])
        if not frozen or any(evidence.get(k) != frozen.get(k) for k in ('text', 'version_id', 'locator')):
            raise ValueError('实际上下文与冻结证据内容不一致')
    ranking = row.get('trace', {}).get('ranking', {})
    sources = {'candidates': list(dict.fromkeys(ranking.get('bm25', []) + ranking.get('vector', []))),
               'fused': ranking.get('fused'), 'reranked': ranking.get('reranked'),
               'context': [c['id'] for c in row.get('evidence', [])]}
    stages = {}
    for stage, ids in sources.items():
        if ids is None or (stage == 'candidates' and not ranking):
            stages[stage] = None
            continue
        if any(i not in chunks for i in ids):
            raise ValueError('运行记录包含冻结语料之外的证据')
        evidence = [chunks[i] for i in ids]
        hits = [any(covered(a, evidence, corpus) for a in group) for group in case.evidence_groups]
        stages[stage] = {'hits': hits, 'coverage': sum(hits) / len(hits), 'full': all(hits)}
    losses, previous = [], [True] * len(case.evidence_groups)
    for stage in ('candidates', 'fused', 'reranked', 'context'):
        value = stages[stage]
        if value is None:
            continue
        lost = [i for i, (before, after) in enumerate(zip(previous, value['hits'])) if before and not after]
        if lost:
            losses.append({'stage': stage, 'groups': lost})
        previous = value['hits']
    primary = losses[0]['stage'] if losses else 'execution_failure' if row.get('status') == 'error' else 'answer_composition' if row.get('human', {}).get('correct') is False else 'needs_review'
    # 旧记录缺少裁剪前序列时只能定位重排/选择区间，不能补造预算原因。
    if stages['reranked'] is None and stages.get('fused') and stages.get('context'):
        lost = [i for i, (a, b) in enumerate(zip(stages['fused']['hits'], stages['context']['hits'])) if a and not b]
        if lost:
            losses = [x for x in losses if x['stage'] != 'context'] + [{'stage': 'rerank_or_selection', 'groups': lost}]
            if primary == 'context':
                primary = 'rerank_or_selection'
    context_reasons = []
    for reason in sorted({d['reason'] for d in ranking.get('context', {}).get('dropped', [])}):
        dropped = [chunks[d['id']] for d in ranking['context']['dropped'] if d['reason'] == reason]
        groups = [i for i, group in enumerate(case.evidence_groups) if not stages['context']['hits'][i] and
                  any(covered(a, dropped + row.get('evidence', []), corpus) for a in group)]
        if groups:
            context_reasons.append({'reason': reason, 'groups': groups})
    return {'stages': stages, 'primary': primary, 'losses': losses, 'context_loss_reasons': context_reasons,
            'confidence': 'anchor_loss' if losses else 'needs_review',
            'semantic_review_required': True, 'rerank_complete': ranking.get('rerank_complete'),
            'execution_error': row.get('error')}
