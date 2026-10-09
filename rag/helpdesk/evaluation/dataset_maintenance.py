import difflib
from datetime import datetime, timezone
from .dataset import Case, same_locator, anchor_interval

def changes(old, new):
    old_text = '\n'.join(p['text'] for p in old['paragraphs'])
    new_text = '\n'.join(p['text'] for p in new['paragraphs'])
    return '\n'.join(difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), fromfile='old', tofile='new', lineterm=''))

def affected(cases, old, new):
    old_versions = {p['version_id'] for p in old['paragraphs']}
    new_versions = {p['version_id'] for p in new['paragraphs']}
    changed = old_versions ^ new_versions
    result = []
    for c in cases:
        reasons = []
        if any(a.source_version in changed for group in c.evidence_groups for a in group):
            reasons.append('来源版本变化')
        if c.expected_behavior != 'answer':
            reasons.append('新增资料可能改变拒答/澄清真值')
        if len(c.evidence_groups) > 1 or 'conflict' in c.tags or 'scope' in c.tags:
            reasons.append('跨文档/冲突/适用范围需复核')
        # 保守检查其余题目，新增文档也可能形成替代证据或冲突。
        if changed and not reasons:
            reasons.append('知识快照变化，需检查替代来源与新增冲突')
        if reasons:
            result.append({'id': c.id, 'reasons': reasons})
    return result

async def suggest(models, cases, old, new, ids):
    proposals = []
    for case in cases:
        if case.id not in ids:
            continue
        proposal = await models.json('你辅助维护 RAG 黄金集。仅依据旧/新原文提出修改，禁止依据被测系统的回答。输出 {"action":"keep|reanchor|revise|retire","reason":"...","case":{完整样本字段}}。必要事实逐项核对；证据 quote 必须原样摘录且 locator 指向新原文。输出只是待人工审核建议。',
                                     {'case': case.model_dump(), 'old_paragraphs': old['paragraphs'], 'new_paragraphs': new['paragraphs'], 'new_corpus_version': new['corpus_version']})
        data = proposal.get('case', {})
        data.update(id=case.id, case_revision=case.case_revision+1, status='review', review_record={},
                    change_reason=proposal.get('reason','AI 建议'), corpus_version=new['corpus_version'])
        if proposal.get('action') == 'retire':
            data['status'] = 'retired'
        candidate = Case.model_validate(data)
        if candidate.status != 'retired':
            for group in candidate.evidence_groups:
                for anchor in group:
                    anchor_interval(anchor, new)
        proposals.append({'action': proposal.get('action'), 'before': case.model_dump(), 'after': candidate.model_dump()})
    return proposals

def review(case, reviewer):
    data = case.model_dump()
    data['status'] = 'retired' if case.status == 'retired' else 'active'
    data['review_record'] = {'reviewer': reviewer, 'approved_at': datetime.now(timezone.utc).isoformat()}
    return Case.model_validate(data)
