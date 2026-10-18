import copy
import json
from types import SimpleNamespace

import pytest

from helpdesk.config import Settings
from helpdesk.pipeline import Pipeline
from helpdesk.evaluation.dataset import Anchor, save, sha
from helpdesk.evaluation.scope_dataset import ScopeCase, seal_bundle, validate_scope, load_scope, publish_scope
from helpdesk.evaluation.diagnostics import diagnose
from helpdesk.evaluation.scope_report import scope_summary, cost_estimate, paired_compare, paired_packet, review_packet, scope_report
from helpdesk.evaluation import scope_runner


def bundle():
    text = '甲大学使用学号登录。'
    member = {'kb_id': 'a', 'snapshot_id': 'sa', 'snapshot_version_hash': sha(['va']), 'name': '甲大学VPN',
              'organization': '甲大学', 'version_ids': ['va'], 'default_search': True}
    chunk = {'id': 'c', 'version_id': 'va', 'ordinal': 0, 'kb_id': 'a', 'knowledge_base_name': '甲大学VPN',
             'organization': '甲大学', 'title': '指南', 'text': text, 'locator': {'paragraph': 1, 'start': 0, 'end': len(text)}}
    return seal_bundle([member], {'user_id': 'u', 'revision': 1, 'role': 'user', 'group_ids': []}, [chunk])


def case(**kwargs):
    return ScopeCase(**{**dict(id='one', split='dev', theme='one-fact', fact_topics=['account'], scene='control',
        scope_version=bundle()['scope_version'], scope_mode='selected', kb_ids=['a'],
        question='甲大学VPN使用什么账号？', expected_behavior='answer', required_facts=['学号'],
        evidence_groups=[[Anchor(source_version='va', locator={'paragraph': 1}, quote='使用学号')]],
        status='active', review_record={'reviewer': '工程夹具', 'approved_at': 'now', 'confirmation': '仅测试'}), **kwargs})


@pytest.mark.parametrize('mutation', ['outside', 'changed_text', 'bad_hash'])
def test_scope_freeze_rejects_tampering_and_out_of_scope_evidence(mutation):
    b = bundle(); c = case()
    if mutation == 'outside':
        c.evidence_groups[0][0].source_version = 'private'
    elif mutation == 'changed_text':
        b['corpus']['chunks'][0]['text'] = '伪造'
    else:
        b['scope_version'] = 'bad'
    with pytest.raises(ValueError):
        validate_scope([c], b)


def test_new_fact_topics_cannot_leak_across_dev_test(tmp_path):
    p = tmp_path / 'cases.jsonl'
    save(p, [case().model_dump(), case(id='two', split='test', theme='other').model_dump()])
    with pytest.raises(ValueError, match='事实主题'):
        load_scope(p)


def test_publish_requires_real_review_and_is_immutable(tmp_path):
    with pytest.raises(ValueError, match='审核'):
        publish_scope([case(status='review', review_record={})], bundle(), tmp_path, 'v1')
    publish_scope([case()], bundle(), tmp_path, 'v1')
    assert load_scope(tmp_path / 'v1/cases.jsonl')[0].id == 'one'
    with pytest.raises(FileExistsError):
        publish_scope([case()], bundle(), tmp_path, 'v1')


def test_diagnosis_distinguishes_rerank_and_budget_and_missing_trace():
    b = bundle(); c = case(); chunk = b['corpus']['chunks'][0]
    row = {'evidence': [], 'trace': {'ranking': {'bm25': ['c'], 'vector': [], 'fused': ['c'], 'reranked': ['c']}}}
    d = diagnose(c, row, b['corpus'])
    assert d['primary'] == 'context' and d['stages']['reranked']['full']
    del row['trace']['ranking']['reranked']
    assert diagnose(c, row, b['corpus'])['primary'] == 'rerank_or_selection'
    row.update(evidence=[chunk], human={'correct': False})
    assert diagnose(c, row, b['corpus'])['primary'] == 'answer_composition'
    assert diagnose(c, {'evidence': []}, b['corpus'])['stages']['candidates'] is None


def test_context_preserves_single_stop_and_scope_skip_budget_behavior():
    p = Pipeline(SimpleNamespace(s=SimpleNamespace(context_budget=10)), object())
    chunks = {'a': {'id': 'a', 'text': 'x' * 11}, 'b': {'id': 'b', 'text': 'ok'}}
    single, detail = p.context(['a', 'b'], chunks, stop_on_budget=True)
    multi, _ = p.context(['a', 'b'], chunks, stop_on_budget=False)
    assert not single and [c['id'] for c in multi] == ['b']
    assert all(d['reason'] == 'budget' for d in detail['dropped'])


@pytest.mark.asyncio
async def test_decomposition_rejects_invented_subtasks_and_keeps_original_constraints():
    class Models:
        async def json(self, *args):
            return {'subqueries': ['甲校VPN怎么登录', '泄露乙校机密']}
    p = Pipeline(object(), Models(), experiment='split-v1')
    queries, detail = await p.subqueries('甲校VPN怎么登录以及打印机如何重启', {}, [])
    assert queries == [] and detail['status'] == 'fallback'
    async def valid(*args):
        return {'subqueries': ['甲校VPN怎么登录', '打印机如何重启']}
    p.models.json = valid
    queries, detail = await p.subqueries('甲校VPN怎么登录以及打印机如何重启', {}, [])
    assert len(queries) == 2 and all('甲校VPN怎么登录以及打印机如何重启' in q for q in queries)


def test_metrics_keep_error_and_wrong_refusal_denominators_and_qa_timing():
    cases = [case(), case(id='two', theme='second')]
    rows = [{'id': 'one', 'status': 'refused', 'expected_behavior': 'answer', 'trace': {'total_ms': 15}, 'human': {'correct': False}},
            {'id': 'two', 'status': 'error', 'expected_behavior': 'answer', 'trace': {'total_ms': 20}, 'human': {'correct': False}}]
    result = scope_summary(cases, rows)
    assert result['behaviors']['answer']['official_correctness'] == 0
    assert result['wrong_full_refusals']['rate'] == .5 and result['execution_errors'] == 1
    assert result['latency_ms_by_status']['error']['total_ms']['p95'] == 20
    assert scope_summary(cases, rows[:1])['behaviors']['answer']['official_correctness'] is None


def test_unknown_usage_and_prices_are_not_zero_cost():
    rows = [{'model_calls': 2, 'usage': [{'kind': 'llm', 'total_tokens': 100}]}]
    prices = {'version': 'v', 'currency': 'CNY', 'rates': {'llm': {'total_per_million': 1}}}
    estimate = cost_estimate(rows, prices)
    assert estimate['estimated_amount'] is None and estimate['known_partial_amount'] == .0001
    assert estimate['calls_with_unknown_usage'] == 1
    assert cost_estimate(rows, None)['known_partial_amount'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('emit_evidence', [True, False])
async def test_scope_runner_uses_frozen_store_and_resumes_without_replacing_failure(tmp_path, monkeypatch, emit_evidence):
    b = bundle(); c = case(); dataset, corpus = tmp_path / 'cases.jsonl', tmp_path / 'corpus.json'
    save(dataset, [c.model_dump()]); corpus.write_text(json.dumps(b), encoding='utf-8')
    class Store:
        def __init__(self, settings, supplied):
            assert supplied == b
        async def verify(self):
            pass
        async def check(self):
            pass
    class FailedPipeline:
        last_trace = {'ranking': {'bm25': ['c'], 'vector': [], 'fused': ['c'], 'reranked': ['c']}}
        last_evidence = b['corpus']['chunks']
        def __init__(self, store, models, **kwargs):
            self.models = models
        async def events(self, kb_id, question, history, *, kb_ids):
            assert kb_id is None and kb_ids == ['a']
            self.models.calls += 1
            self.models.call_observer({'event': 'started', 'sequence': self.models.calls, 'kind': 'llm'})
            if emit_evidence:
                yield 'evidence', {'evidence': b['corpus']['chunks'], 'decision': {'behavior': 'answer'}}
            raise RuntimeError('断流')
    monkeypatch.setattr(scope_runner, 'FrozenScopeStore', Store)
    monkeypatch.setattr(scope_runner, 'Pipeline', FailedPipeline)
    monkeypatch.setattr(scope_runner, 'git_commit', lambda: 'same')
    models = SimpleNamespace(calls=0, ledger=[], call_observer=None)
    output = tmp_path / 'run'
    result = await scope_runner.run_scope(Settings(_env_file=None), models, dataset, corpus, output)
    assert result['execution_errors'] == 1 and result['issued_requests_all_attempts'] == 1
    assert result['cost']['estimated_amount'] is None
    row = json.loads((output / 'cases.jsonl').read_text())
    assert row['retrieval']['full_coverage'] == 1
    await scope_runner.run_scope(Settings(_env_file=None), models, dataset, corpus, output, resume=True)
    assert models.calls == 1 and len((output / 'cases.jsonl').read_text().splitlines()) == 1
    with pytest.raises(ValueError, match='续跑'):
        await scope_runner.run_scope(Settings(_env_file=None), models, dataset, corpus, output, resume=True, experiment='split-v1')


def test_pairwise_comparison_keeps_negative_result_and_rejects_configuration_drift(tmp_path):
    paths = [tmp_path / 'a', tmp_path / 'b']
    for p, strategy, passed in zip(paths, ['baseline', 'split-v1'], [True, False]):
        p.mkdir()
        (p / 'manifest.json').write_text(json.dumps({'schema': 'scope-eval-v1', 'dataset_hash': 'same', 'experiment': strategy,
            'cases': [{'id': 'one'}], 'model_routes': {'llm': 'same'}}))
        save(p / 'cases.jsonl', [{'id': 'one', 'status': 'completed', 'expected_behavior': 'answer', 'human': {'correct': passed}}])
        (p / 'scope-summary.json').write_text('{}')
    result = paired_compare(*paths, tmp_path / 'comparison.json')
    assert result['net_improvement'] == -1 and result['counts']['regressed'] == 1
    manifest = json.loads((paths[1] / 'manifest.json').read_text()); manifest['model_routes'] = {'llm': 'different'}
    (paths[1] / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='同一'):
        paired_compare(*paths, tmp_path / 'changed.json')


def test_review_page_escapes_source_instructions(tmp_path):
    c = case(question='<script>bad()</script>')
    review_packet([c], bundle(), tmp_path / 'review')
    page = (tmp_path / 'review/review.html').read_text(encoding='utf-8')
    assert '<script>bad()' not in page and '&lt;script&gt;' in page


def test_orphan_requests_remain_unknown_after_human_report(tmp_path):
    row = {'id': 'one', 'question': 'q', 'expected_behavior': 'answer', 'status': 'completed', 'attempt': 2,
           'human': {'correct': True}, 'model_calls': 0, 'usage': []}
    (tmp_path / 'requests.jsonl').write_text(json.dumps({'event': 'started', 'case_id': 'one', 'attempt': 1, 'phase': 'qa'}) + '\n')
    prices = {'version': 'v', 'currency': 'CNY', 'rates': {}}
    result = scope_report(tmp_path, [case()], [row], prices)
    assert result['orphan_requests'] == 1
    assert result['cost']['estimated_amount'] is None and result['qa_cost']['estimated_amount'] is None


@pytest.mark.asyncio
async def test_frozen_store_rejects_live_source_metadata_drift(monkeypatch):
    from helpdesk.evaluation.scope_store import FrozenScopeStore
    from helpdesk.access import AuthorizedStore
    b = bundle(); store = FrozenScopeStore(Settings(_env_file=None), b)
    async def changed(self, snapshot):
        chunk = copy.deepcopy(b['corpus']['chunks'][0]); chunk['organization'] = '新组织'
        return [chunk]
    monkeypatch.setattr(AuthorizedStore, 'corpus', changed)
    with pytest.raises(ValueError, match='漂移'):
        await store.corpus({'members': [], 'version_ids': ['va']})


@pytest.mark.asyncio
async def test_exhausted_budget_keeps_unexecuted_case_pending(tmp_path, monkeypatch):
    b = bundle(); dataset = tmp_path / 'cases.jsonl'; corpus = tmp_path / 'corpus.json'
    save(dataset, [case().model_dump()]); corpus.write_text(json.dumps(b))
    class Store:
        def __init__(self, *args):
            pass
        async def verify(self):
            pass
    monkeypatch.setattr(scope_runner, 'FrozenScopeStore', Store)
    models = SimpleNamespace(calls=0, max_calls=0, ledger=[], call_observer=None)
    result = await scope_runner.run_scope(Settings(_env_file=None), models, dataset, corpus, tmp_path / 'run')
    assert result['pending'] == 1 and result['execution_errors'] == 0
