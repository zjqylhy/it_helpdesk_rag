import json
from types import SimpleNamespace

import pytest

from helpdesk.config import Settings
from helpdesk.pipeline import Pipeline
from helpdesk.providers import ProviderError
from helpdesk.evaluation import runner
from helpdesk.evaluation.answer_judge import judge
from helpdesk.evaluation.dataset import Anchor, Case, save


@pytest.mark.asyncio
async def test_section_reaches_both_sufficiency_and_answer_without_changing_quote(monkeypatch):
    original = {'id': 'c', 'title': '邮箱帮助', 'text': '请用户重新安装APP，再重试。',
                'locator': {'section': 'APP提示账号已存在', 'paragraph': 19}}
    captured = {}

    class SourceModels:
        s = Settings(_env_file=None)

        async def json(self, system, payload):
            captured['sufficiency'] = payload
            return {'behavior': 'answer', 'reason': '章节与问题匹配', 'evidence_labels': ['C1']}

        async def stream(self, messages):
            captured['answer'] = json.loads(messages[-1]['content'])
            yield {'text': '重新安装APP后重试 [C1]。'}

    class SourceStore:
        async def snapshot(self, *args):
            return {'id': 'snapshot'}

    async def retrieve(*args):
        return [original], {'final': ['c']}

    pipeline = Pipeline(SourceStore(), SourceModels())
    monkeypatch.setattr(pipeline, 'retrieve', retrieve)
    events = [event async for event in pipeline.events('kb', 'APP提示账号已存在该怎么办？')]
    for stage in ('sufficiency', 'answer'):
        source = captured[stage]['evidence'][0]
        assert source['section'] == original['locator']['section']
        assert source['text'] == original['text'] and source['title'] == original['title']
    assert events[-1][1]['status'] == 'completed'
    assert next(data for event, data in events if event == 'citations')['citations'][0]['text'] == original['text']


@pytest.mark.asyncio
async def test_answer_failure_preserves_recall_and_failure_denominator(tmp_path, monkeypatch):
    text = '请用户重新安装APP，再重试。'
    locator = {'section': 'APP提示账号已存在', 'paragraph': 19}
    chunk = {'id': 'c', 'version_id': 'v', 'title': '邮箱帮助', 'text': text,
             'locator': {**locator, 'start': 0, 'end': len(text)}}
    corpus = {'knowledge_base': 'kb', 'corpus_version': 'snapshot', 'chunks': [chunk],
              'paragraphs': [{'version_id': 'v', 'locator': locator, 'text': text}]}
    case = Case(id='case', split='dev', theme='account', status='active',
                review_record={'reviewer': 'human', 'approved_at': '2026-10-10'},
                knowledge_base='kb', corpus_version='snapshot', question='账号已存在怎么办？',
                expected_behavior='answer', required_facts=['重新安装APP后重试。'],
                evidence_groups=[[Anchor(source_version='v', locator=locator, quote='重新安装APP')]])
    dataset_path, corpus_path = tmp_path / 'cases.jsonl', tmp_path / 'corpus.json'
    save(dataset_path, [case])
    corpus_path.write_text(json.dumps(corpus), encoding='utf-8')

    class SourceStore:
        async def export_snapshot(self, *args):
            return corpus

    class FailedAnswer:
        def __init__(self, *args):
            pass

        async def events(self, *args):
            yield 'evidence', {'evidence': [chunk], 'decision': {'behavior': 'answer'}}
            yield 'answer_delta', {'text': '重新安装APP后重试。'}
            raise ProviderError('引用校验失败')

    monkeypatch.setattr(runner, 'Pipeline', FailedAnswer)
    monkeypatch.setattr(runner, 'git_commit', lambda: 'contract')
    models = SimpleNamespace(calls=0, ledger=[])
    output = tmp_path / 'run'
    result = await runner.run(Settings(_env_file=None), SourceStore(), models, dataset_path, corpus_path,
                              output, mode='end-to-end', judge_answers=True)
    row = json.loads((output / 'cases.jsonl').read_text(encoding='utf-8'))
    assert result['recall_at_5'] == 1 and result['execution_errors'] == 1
    assert result['automatic_answer_correctness'] == 0 and result['answer_citation_coverage'] == 0
    assert row['status'] == 'error' and row['trace']['total_ms'] >= 0
    assert row['evidence'] == [chunk] and row['answer'] == '重新安装APP后重试。'


@pytest.mark.asyncio
async def test_unexpected_behavior_still_requires_human_review():
    case = Case(id='case', split='dev', theme='conditions', knowledge_base='kb', corpus_version='s',
                question='条件不明确时能否确定答案？', expected_behavior='clarify')
    result = await judge(SimpleNamespace(), case, {'status': 'completed'})
    assert result['correct'] is False and result['needs_review'] is True
