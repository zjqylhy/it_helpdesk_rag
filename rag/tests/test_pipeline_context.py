import json
from types import SimpleNamespace

import pytest

from helpdesk.config import Settings
from helpdesk.pipeline import Pipeline
from helpdesk.providers import ProviderError
from helpdesk.evaluation import runner, cli
from helpdesk.evaluation.answer_judge import judge, JUDGE_VERSION
from helpdesk.evaluation.dataset import Anchor, Case, save, sha


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


@pytest.mark.asyncio
async def test_rejudge_records_new_judge_and_keeps_generation_and_original_result(tmp_path, monkeypatch):
    case = Case(id='case', split='dev', theme='account', knowledge_base='kb', corpus_version='s',
                question='账号验证有什么条件？', expected_behavior='answer', required_facts=['账号有效并在指定设备验证。'],
                evidence_groups=[[Anchor(source_version='v', locator={'paragraph': 1}, quote='账号条件')]])
    dataset = tmp_path / 'dataset.jsonl'
    save(dataset, [case])
    source, output = tmp_path / 'source', tmp_path / 'rejudged'
    source.mkdir()
    manifest = {'dataset_hash': sha([case.model_dump()]), 'split': 'dev', 'commit': 'generation-commit',
                'prompt': 'qa-old', 'cases': [{'id': case.id, 'expected_behavior': 'answer'}]}
    (source / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    row = {'id': case.id, 'question': case.question, 'expected_behavior': 'answer', 'status': 'completed',
           'answer': '使用有效账号完成验证 [C1]。', 'evidence': [{'id': 'c', 'text': '账号条件'}],
           'citations': [{'id': 'c', 'label': 'C1'}], 'trace': {'total_ms': 1}, 'judge': {'correct': True}}
    save(source / 'cases.jsonl', [row])

    class SourceModels:
        calls = 0
        ledger = []

        async def close(self):
            pass

    async def new_judge(models, case, saved):
        models.calls += 1
        models.ledger.append({'kind': 'llm', 'total_tokens': 1})
        return {'correct': False, 'needs_review': True, 'reason': '未覆盖设备条件'}

    monkeypatch.setattr(cli, 'Settings', lambda: Settings(_env_file=None, llm_model='configured-model'))
    monkeypatch.setattr(cli, 'Store', lambda _: object())
    monkeypatch.setattr(cli, 'Models', lambda _: SourceModels())
    monkeypatch.setattr(cli, 'judge', new_judge)
    monkeypatch.setattr(cli, 'git_commit', lambda: 'judge-new')
    await cli.execute(SimpleNamespace(command='judge', dataset=str(dataset), run=str(source), out=str(output),
                                      max_calls=1, ragas=False))
    actual_manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    actual_row = json.loads((output / 'cases.jsonl').read_text(encoding='utf-8'))
    assert actual_manifest['commit'] == 'generation-commit' and actual_manifest['prompt'] == 'qa-old'
    assert actual_manifest['judge_commit'] == 'judge-new' and actual_manifest['judge_prompt'] == JUDGE_VERSION
    assert actual_manifest['judge_llm'] == 'configured-model' and actual_manifest['judge_source_manifest_hash'] == sha(manifest)
    assert actual_row['judge']['correct'] is False
    for key in ('answer', 'evidence', 'citations'):
        assert actual_row[key] == row[key]
    assert json.loads((source / 'cases.jsonl').read_text(encoding='utf-8'))['judge']['correct'] is True
