import json
from types import SimpleNamespace

import pytest

from helpdesk.pipeline import Pipeline
from helpdesk.providers import ProviderError
from helpdesk.scope import understand_scope
from helpdesk.store import Store, combine_snapshots


def snapshot():
    return combine_snapshots([
        {'kb_id': 'a', 'id': 'sa', 'name': '甲大学 VPN', 'organization': '甲大学', 'version_ids': ['va']},
        {'kb_id': 'b', 'id': 'sb', 'name': '乙大学 VPN', 'organization': '乙大学', 'version_ids': ['vb']},
    ])


class Models:
    s = SimpleNamespace(llm_model='mock', embedding_model='mock', rerank_model='mock')

    def __init__(self, result):
        self.result = result
        self.usage = []
        self.payloads = []
        self.embeddings = 0

    async def json(self, system, payload):
        self.payloads.append(payload)
        if 'available_knowledge_bases' in payload:
            return self.result
        return {'behavior': 'answer', 'reason': '完整证据', 'evidence_labels': ['C1', 'C2']}

    async def embed(self, texts, text_type):
        self.embeddings += 1
        return [[1, 0]]

    async def rerank(self, query, documents, top_n):
        # 模拟所有甲校候选都比乙校高分，验证比较对象仍能保留证据。
        ranked = sorted(range(len(documents)), key=lambda i: ('乙大学' in documents[i], i))
        return [{'index': i, 'relevance_score': 1 / (rank + 1)} for rank, i in enumerate(ranked)]

    async def stream(self, messages):
        self.generated = json.loads(messages[-1]['content'])
        yield {'text': '甲校步骤 [C1]；乙校步骤 [C2]。'}


class SourceStore:
    s = SimpleNamespace(context_budget=8000)

    async def scope_snapshot(self, kb_ids):
        self.selected = kb_ids
        bound = snapshot()
        return combine_snapshots([m for m in bound['members'] if not kb_ids or m['kb_id'] in kb_ids])

    async def corpus(self, snap):
        return [{'id': f'{kb}{i}', 'kb_id': kb, 'knowledge_base_name': name, 'organization': name,
                 'title': 'VPN指南', 'text': f'{name}登录步骤{i}', 'locator': {'paragraph': i}, 'version_id': 'v' + kb}
                for kb, name, count in [('a', '甲大学', 8), ('b', '乙大学', 1)]
                for i in range(count) if 'v' + kb in snap['version_ids']]

    async def vector_search(self, snap, vector):
        return [{'id': c['id']} for c in await self.corpus(snap)]


@pytest.mark.asyncio
async def test_vpn_clarification_does_not_retrieve_or_generate():
    models = Models({'query': 'VPN怎么登录', 'behavior': 'clarify', 'message': '请问你属于哪个学校？',
                     'option_ids': ['a', 'b'], 'kb_ids': [], 'requires_all': False})
    pipeline = Pipeline(SourceStore(), models)
    events = [e async for e in pipeline.events(None, 'VPN怎么登录', kb_ids=[])]
    assert events[-1][1]['status'] == 'clarify'
    assert models.embeddings == 0 and not hasattr(models, 'generated')
    text = next(d['text'] for event, d in events if event == 'refusal')
    assert '甲大学' in text and '乙大学' in text
    assert events[-1][1]['trace']['snapshot_members'] == snapshot()['members']


@pytest.mark.asyncio
async def test_school_reply_receives_clarification_history_and_narrows_snapshot():
    history = [{'role': 'user', 'content': 'VPN怎么登录'},
               {'role': 'assistant', 'content': '请问你属于哪个学校？', 'status': 'clarify'}]
    models = Models({'query': '甲大学VPN怎么登录', 'behavior': 'search', 'kb_ids': ['a'], 'requires_all': False})
    narrowed, query, text, required = await understand_scope(models, snapshot(), '甲大学', history)
    assert models.payloads[0]['history'] == history
    assert narrowed['version_ids'] == ['va'] and query == '甲大学VPN怎么登录'
    assert text is None and required == []


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['kb_ids', 'option_ids'])
async def test_scope_result_cannot_select_outside_bound_sources(field):
    result = {'query': '问题', 'behavior': 'search', field: ['excluded']}
    with pytest.raises(ProviderError, match='超出'):
        await understand_scope(Models(result), snapshot(), '问题', [])


@pytest.mark.asyncio
async def test_cross_library_comparison_preserves_each_library_and_query_embedding_once():
    models = Models({'query': '比较甲乙VPN登录', 'behavior': 'search', 'kb_ids': ['a', 'b'], 'requires_all': True})
    events = [e async for e in Pipeline(SourceStore(), models).events(None, '比较甲乙VPN登录', kb_ids=['a', 'b'])]
    assert events[-1][1]['status'] == 'completed'
    citations = next(d['citations'] for event, d in events if event == 'citations')
    assert {c['kb_id'] for c in citations} == {'a', 'b'}
    assert models.embeddings == 1
    assert {e['organization'] for e in models.generated['evidence']} == {'甲大学', '乙大学'}


@pytest.mark.asyncio
async def test_comparison_missing_citation_is_not_a_completed_answer():
    models = Models({'query': '比较甲乙VPN登录', 'behavior': 'search', 'kb_ids': ['a', 'b'], 'requires_all': True})
    async def partial(messages):
        yield {'text': '只有甲校 [C1]。'}
    models.stream = partial
    with pytest.raises(ProviderError, match='未引用全部'):
        _ = [e async for e in Pipeline(SourceStore(), models).events(None, '比较甲乙VPN登录')]


@pytest.mark.asyncio
async def test_bound_comparison_missing_library_evidence_refuses():
    class MissingStore(SourceStore):
        async def corpus(self, snap):
            return [c for c in await super().corpus(snap) if c['kb_id'] != 'b']
    models = Models({'query': '比较甲乙VPN登录', 'behavior': 'search', 'kb_ids': ['a', 'b'], 'requires_all': True})
    events = [e async for e in Pipeline(MissingStore(), models).events(None, '比较甲乙VPN登录')]
    assert events[-1][1]['status'] == 'refused' and not hasattr(models, 'generated')


@pytest.mark.asyncio
async def test_snapshot_query_uses_default_filter_and_checks_explicit_ids():
    store = Store(SimpleNamespace())
    captured = []
    async def rows(sql, args):
        captured.append((sql, args))
        return snapshot()['members']
    store.rows = rows
    bound = await store.scope_snapshot([])
    assert captured[-1][1] == ([], True) and 'k.default_search' in captured[-1][0]
    assert bound['version_ids'] == ['va', 'vb']
    await store.scope_snapshot(['a', 'b'])
    assert captured[-1][1] == (['a', 'b'], False)
    with pytest.raises(ValueError):
        await store.scope_snapshot(['missing'])


def test_composite_snapshot_identity_is_order_independent_and_changes_on_publication():
    original = snapshot()
    assert combine_snapshots(original['members'][::-1])['id'] == original['id']
    updated = [dict(m) for m in original['members']]
    updated[0]['id'] = 'new-publication'
    assert combine_snapshots(updated)['id'] != original['id']


@pytest.mark.asyncio
async def test_scope_metadata_does_not_change_frozen_single_library_export():
    store = Store(SimpleNamespace(embedding_dimension=2, embedding_model='mock'))
    source = {'id': 'a', 'embedding_model': 'mock', 'dimension': 2, 'kb_id': 'kb',
              'knowledge_base_name': '学校', 'organization': '学校', 'text': '原文'}
    async def rows(*args):
        return [source]
    store.rows = rows
    legacy = await store.corpus({'version_ids': ['va']})
    combined = await store.corpus({'version_ids': ['va'], 'members': []})
    assert 'kb_id' not in legacy[0] and 'knowledge_base_name' not in legacy[0]
    assert combined[0] == source


@pytest.mark.asyncio
async def test_explicit_single_library_bypasses_organization_clarification():
    models = Models({'query': 'VPN怎么登录', 'behavior': 'clarify', 'message': '请问哪个学校？',
                     'kb_ids': [], 'option_ids': ['a'], 'requires_all': False})
    events = [e async for e in Pipeline(SourceStore(), models).events(None, 'VPN怎么登录', kb_ids=['a'])]
    assert events[-1][1]['status'] == 'completed'
    assert all('available_knowledge_bases' not in p for p in models.payloads)
    assert '甲大学' in events[-1][1]['trace']['query']
    citations = next(d['citations'] for event, d in events if event == 'citations')
    assert {c['kb_id'] for c in citations} == {'a'}


@pytest.mark.asyncio
async def test_unknown_organization_refuses_without_searching_similar_school():
    models = Models({'query': '丙大学VPN怎么登录', 'behavior': 'refuse', 'message': '当前范围没有丙大学的VPN资料。',
                     'kb_ids': [], 'option_ids': [], 'requires_all': False})
    events = [e async for e in Pipeline(SourceStore(), models).events(None, '丙大学VPN怎么登录')]
    assert events[-1][1]['status'] == 'refused' and models.embeddings == 0
    assert '丙大学' in next(d['text'] for event, d in events if event == 'refusal')


@pytest.mark.asyncio
async def test_joint_retrieval_cannot_be_mislabeled_as_single_library_ablation():
    with pytest.raises(ValueError, match='联合问答仅支持'):
        _ = [e async for e in Pipeline(SourceStore(), Models({})).events(None, '问题', variant='A')]
