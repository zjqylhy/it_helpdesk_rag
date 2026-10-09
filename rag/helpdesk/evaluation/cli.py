import argparse
import asyncio
import json
from pathlib import Path
from .dataset import load, save, validate, publish, Case, sha
from .dataset_maintenance import changes, affected, suggest, review
from .report import compare, report
from .runner import run
from .answer_judge import judge
from ..config import Settings
from ..store import Store
from ..providers import Models

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def write(path,data):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')

def parser():
    p = argparse.ArgumentParser(description='知识问答评测与数据集维护（私有文件请保存在 eval/private/）')
    sub=p.add_subparsers(dest='command',required=True)
    x=sub.add_parser('export');x.add_argument('--kb',required=True);x.add_argument('--snapshot');x.add_argument('--out',required=True)
    x=sub.add_parser('validate');x.add_argument('dataset');x.add_argument('--corpus',required=True)
    x=sub.add_parser('draft');x.add_argument('dataset');x.add_argument('--out',required=True)
    x=sub.add_parser('edit');x.add_argument('dataset');x.add_argument('--case',required=True,help='完整样本 JSON，保留 ID 并增加 revision');x.add_argument('--out',required=True)
    x=sub.add_parser('review');x.add_argument('dataset');x.add_argument('--id',required=True);x.add_argument('--reviewer',required=True);x.add_argument('--out',required=True)
    x=sub.add_parser('publish');x.add_argument('dataset');x.add_argument('--corpus',required=True);x.add_argument('--directory',required=True);x.add_argument('--version',required=True)
    x=sub.add_parser('diff');x.add_argument('dataset');x.add_argument('--old',required=True);x.add_argument('--new',required=True);x.add_argument('--out',required=True)
    x=sub.add_parser('suggest');x.add_argument('dataset');x.add_argument('--old',required=True);x.add_argument('--new',required=True);x.add_argument('--out',required=True);x.add_argument('--ids',required=True,help='逗号分隔待修订 ID');x.add_argument('--max-calls',type=int,default=20)
    x=sub.add_parser('generate');x.add_argument('--corpus',required=True);x.add_argument('--count',type=int,default=5);x.add_argument('--split',choices=['dev','test'],default='dev');x.add_argument('--out',required=True)
    x=sub.add_parser('run');x.add_argument('dataset');x.add_argument('--corpus',required=True);x.add_argument('--out',required=True);x.add_argument('--variant',choices=list('ABCD')+['all'],default='D');x.add_argument('--mode',choices=['retrieval','end-to-end'],default='retrieval');x.add_argument('--split',choices=['dev','test'],default='dev');x.add_argument('--resume',action='store_true');x.add_argument('--judge',action='store_true');x.add_argument('--ragas',action='store_true');x.add_argument('--max-calls',type=int,default=200)
    x=sub.add_parser('judge');x.add_argument('dataset');x.add_argument('--run',required=True);x.add_argument('--out',required=True);x.add_argument('--max-calls',type=int,default=200)
    x.add_argument('--ragas',action='store_true')
    x=sub.add_parser('adjudicate');x.add_argument('--run',required=True);x.add_argument('--decisions',required=True);x.add_argument('--out',required=True)
    x=sub.add_parser('compare');x.add_argument('left');x.add_argument('right');x.add_argument('--out',required=True)
    x=sub.add_parser('report');x.add_argument('directory')
    return p

async def execute(args):
    settings=Settings();store=Store(settings);models=Models(settings)
    models.max_calls = getattr(args,'max_calls',200)
    try:
        c=args.command
        if c=='export': write(args.out,await store.export_snapshot(args.kb,args.snapshot))
        elif c=='validate': validate(load(args.dataset),read(args.corpus));print('校验通过')
        elif c=='draft':
            cases=load(args.dataset)
            for item in cases:
                if item.status!='retired': item.status='draft';item.review_record={}
            save(args.out,cases,exclusive=True)
        elif c=='edit':
            cases=load(args.dataset);candidate=Case.model_validate(read(args.case));old=next((x for x in cases if x.id==candidate.id),None)
            if old and candidate.case_revision != old.case_revision+1: raise ValueError('修改样本必须增加一次 revision')
            candidate.status='retired' if candidate.status=='retired' else 'review';candidate.review_record={}
            save(args.out,[candidate if x.id==candidate.id else x for x in cases]+([] if old else [candidate]),exclusive=True)
        elif c=='review':
            cases=load(args.dataset)
            if args.id not in {x.id for x in cases}: raise ValueError('样本 ID 不存在')
            save(args.out,[review(x,args.reviewer) if x.id==args.id else x for x in cases],exclusive=True)
        elif c=='publish': print(json.dumps(publish(args.dataset,read(args.corpus),args.directory,args.version),ensure_ascii=False))
        elif c=='diff':
            old,new=read(args.old),read(args.new)
            write(args.out,{'diff':changes(old,new),'affected':affected(load(args.dataset),old,new),'new_corpus_version':new['corpus_version']})
        elif c=='suggest': write(args.out,await suggest(models,load(args.dataset),read(args.old),read(args.new),set(args.ids.split(','))))
        elif c=='generate':
            if not 1<=args.count<=20: raise ValueError('每次生成 1–20 条候选，分批审核')
            corpus=read(args.corpus)
            generated=await models.json('从提供的真实原文生成黄金集候选，不虚构制度。输出 {"cases":[完整样本]}。每题含 id、split、theme、question、expected_behavior、required_facts、reference_answer、evidence_groups（数组的数组，组内 source_version/locator/quote）及 knowledge_base/corpus_version。每题单独事实主题。证据 quote 原样摘录。仅生成 answer 候选，后续人工补拒答、冲突、多轮场景。',{'corpus':corpus,'count':args.count,'split':args.split})
            candidates=[]
            for row in generated.get('cases',[]):
                row.update(status='review',case_revision=1,review_record={},knowledge_base=corpus['knowledge_base'],corpus_version=corpus['corpus_version'],split=args.split)
                candidates.append(Case.model_validate(row))
            if len(candidates)!=args.count:raise ValueError('候选数量不符合要求')
            validate(candidates,corpus);save(args.out,candidates,exclusive=True)
        elif c=='run':
            if args.variant=='all':
                queries=None
                if args.mode=='retrieval':
                    from ..pipeline import Pipeline
                    pipeline=Pipeline(store,models);querypath=Path(args.out)/'queries.json'
                    if args.resume and querypath.exists(): queries=read(querypath)
                    else:
                        queries={case.id:await pipeline.rewrite(case.question,case.history) for case in load(args.dataset) if case.split==args.split and case.status!='retired'}
                        write(querypath,queries)
                for variant in 'ABCD':
                    print(variant,json.dumps(await run(settings,store,models,args.dataset,args.corpus,Path(args.out)/variant,variant,args.mode,args.resume,args.split,queries,args.judge,args.ragas),ensure_ascii=False))
            else: print(json.dumps(await run(settings,store,models,args.dataset,args.corpus,args.out,args.variant,args.mode,args.resume,args.split,judge_answers=args.judge,ragas=args.ragas),ensure_ascii=False))
        elif c=='judge':
            root=Path(args.out)
            cases={x.id:x for x in load(args.dataset)};rows=[json.loads(x) for x in (Path(args.run)/'cases.jsonl').read_text(encoding='utf-8').splitlines() if x.strip()]
            latest={r['id']:r for r in rows};manifest=read(Path(args.run)/'manifest.json');manifest['judge']=True
            expected=[x for x in cases.values() if x.split==manifest['split'] and x.status!='retired']
            if sha([x.model_dump() for x in expected])!=manifest['dataset_hash']:raise ValueError('评分数据集与原运行清单不一致')
            if root.exists():raise ValueError('评分输出目录已经存在')
            root.mkdir(parents=True,exist_ok=False)
            write(root/'manifest.json',manifest)
            for row in latest.values():
                call_start,usage_start=models.calls,len(models.ledger)
                try: row['judge']=await judge(models,cases[row['id']],row)
                except Exception as exc:row['judge']={'correct':False,'needs_review':True,'error':type(exc).__name__}
                if args.ragas and row['status']=='completed':
                    from .ragas_adapter import faithfulness
                    try:row['faithfulness']=await faithfulness(models,row)
                    except Exception as exc:row['faithfulness_error']=type(exc).__name__
                row['judge_calls']=models.calls-call_start;row['judge_usage']=models.ledger[usage_start:] or None
            save(root/'cases.jsonl',list(latest.values()),exclusive=True);print(json.dumps(report(root,list(latest.values())),ensure_ascii=False))
        elif c=='adjudicate':
            root=Path(args.out);root.mkdir(parents=True,exist_ok=False)
            rows=[json.loads(x) for x in (Path(args.run)/'cases.jsonl').read_text(encoding='utf-8').splitlines() if x.strip()]
            latest={r['id']:r for r in rows}
            decisions=[json.loads(x) for x in Path(args.decisions).read_text(encoding='utf-8').splitlines() if x.strip()]
            for d in decisions:
                if d.get('id') not in latest or type(d.get('correct')) is not bool or not d.get('reviewer') or not d.get('reason'):raise ValueError('人工判定需有效 id、correct 布尔值、reviewer 和 reason')
                latest[d['id']]['human']=d
            write(root/'manifest.json',read(Path(args.run)/'manifest.json'))
            save(root/'cases.jsonl',list(latest.values()),exclusive=True);print(json.dumps(report(root,list(latest.values())),ensure_ascii=False))
        elif c=='compare': compare(args.left,args.right,args.out)
        elif c=='report':
            rows=[json.loads(x) for x in (Path(args.directory)/'cases.jsonl').read_text(encoding='utf-8').splitlines() if x.strip()]
            print(json.dumps(report(args.directory,list({r['id']:r for r in rows}.values())),ensure_ascii=False))
    finally: await models.close()

def main():
    from ..runtime import loop_factory
    try: asyncio.run(execute(parser().parse_args()),loop_factory=loop_factory)
    except Exception as exc: raise SystemExit(f'{type(exc).__name__}: {exc}')

if __name__=='__main__': main()
