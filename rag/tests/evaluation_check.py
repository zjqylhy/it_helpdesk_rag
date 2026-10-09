"""用工程测试快照验证评测 CLI；生成的样本不可用作项目质量真值。"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from helpdesk.config import Settings
from helpdesk.store import Store
from helpdesk.runtime import loop_factory
from helpdesk.evaluation.dataset import Case,Anchor,save

async def prepare(root):
    context=json.loads(Path('../data/integration-context.json').read_text())
    corpus=await Store(Settings()).export_snapshot(context['kb'])
    (root/'corpus.json').write_text(json.dumps(corpus,ensure_ascii=False),encoding='utf-8')
    paragraph=corpus['paragraphs'][0]
    case=Case(id='contract-one',split='dev',theme='contract-topic',status='active',review_record={'reviewer':'工程契约测试','approved_at':'2026-10-09'},
              knowledge_base=context['kb'],corpus_version=corpus['corpus_version'],question='新版本如何连接？',expected_behavior='answer',required_facts=['填写组织提供的服务器地址'],
              evidence_groups=[[Anchor(source_version=paragraph['version_id'],locator=paragraph['locator'],quote=paragraph['text'])]])
    save(root/'cases.jsonl',[case],exclusive=True)

def main():
    import uuid
    root=Path('../data/eval-check-'+uuid.uuid4().hex[:8]);root.mkdir(parents=True)
    asyncio.run(prepare(root),loop_factory=loop_factory)
    def cli(*args):
        result=subprocess.run([sys.executable,'-m','helpdesk.evaluation.cli',*map(str,args)],capture_output=True,text=True,encoding='utf-8')
        if result.returncode:raise AssertionError(result.stdout+result.stderr)
        return result
    cli('validate',root/'cases.jsonl','--corpus',root/'corpus.json')
    cli('publish',root/'cases.jsonl','--corpus',root/'corpus.json','--directory',root/'releases','--version','v1')
    cli('run',root/'cases.jsonl','--corpus',root/'corpus.json','--out',root/'retrieval','--variant','all','--max-calls','20')
    cli('compare',root/'retrieval/A',root/'retrieval/D','--out',root/'comparison.md')
    cli('run',root/'cases.jsonl','--corpus',root/'corpus.json','--out',root/'answers','--mode','end-to-end','--judge','--max-calls','20')
    summary=json.loads((root/'answers/summary.json').read_text())
    assert summary['total']==1 and summary['execution_errors']==0 and summary['automatic_answer_correctness']==1 and summary['official_answer_correctness'] is None,summary
    # 同一清单续跑不产生重复结果。
    cli('run',root/'cases.jsonl','--corpus',root/'corpus.json','--out',root/'answers','--mode','end-to-end','--judge','--resume','--max-calls','20')
    assert len((root/'answers/cases.jsonl').read_text().splitlines())==1
    cli('judge',root/'cases.jsonl','--run',root/'answers','--out',root/'judge-only','--max-calls','20')
    (root/'decisions.jsonl').write_text(json.dumps({'id':'contract-one','correct':True,'reviewer':'工程契约测试','reason':'仅验证人工裁决导入'},ensure_ascii=False)+'\n',encoding='utf-8')
    cli('adjudicate','--run',root/'answers','--decisions',root/'decisions.jsonl','--out',root/'human-reviewed')
    reviewed=json.loads((root/'human-reviewed/summary.json').read_text())
    assert reviewed['official_answer_correctness']==1
    changed=json.loads((root/'cases.jsonl').read_text());changed['question']='修改后问题';save(root/'changed.jsonl',[changed],exclusive=True)
    mismatch=subprocess.run([sys.executable,'-m','helpdesk.evaluation.cli','judge',str(root/'changed.jsonl'),'--run',str(root/'answers'),'--out',str(root/'invalid-judge')],capture_output=True)
    assert mismatch.returncode!=0 and not (root/'invalid-judge').exists()
    print('评测 CLI 验证通过：来源校验、冻结发布、四组检索、完整生成与评分、报告、清单一致续跑。')

if __name__=='__main__':main()
