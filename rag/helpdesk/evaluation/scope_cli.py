"""scope 命令与旧单库 CLI 隔离，离线审核不初始化模型客户端。"""
import json
from pathlib import Path

from .dataset import load, save, sha
from .scope_dataset import load_scope, validate_scope, review_scope, publish_scope
from .scope_report import review_packet, paired_packet, paired_compare, scope_report, write_json
from .diagnostics import diagnose


def register(sub):
    x = sub.add_parser('scope-export'); x.add_argument('--user', required=True); x.add_argument('--out', required=True)
    x = sub.add_parser('scope-validate'); x.add_argument('dataset'); x.add_argument('--corpus', required=True)
    x = sub.add_parser('scope-review'); x.add_argument('dataset'); x.add_argument('--ids', required=True); x.add_argument('--reviewer', required=True); x.add_argument('--confirmation', required=True); x.add_argument('--packet', required=True); x.add_argument('--out', required=True)
    x = sub.add_parser('scope-publish'); x.add_argument('dataset'); x.add_argument('--corpus', required=True); x.add_argument('--directory', required=True); x.add_argument('--version', required=True)
    x = sub.add_parser('scope-packet'); x.add_argument('dataset'); x.add_argument('--corpus', required=True); x.add_argument('--out', required=True); x.add_argument('--run')
    x = sub.add_parser('scope-run'); x.add_argument('dataset'); x.add_argument('--corpus', required=True); x.add_argument('--out', required=True); x.add_argument('--split', choices=['dev', 'test'], default='dev'); x.add_argument('--experiment', choices=['baseline', 'split-v1'], default='baseline'); x.add_argument('--resume', action='store_true'); x.add_argument('--judge', action='store_true'); x.add_argument('--prices'); x.add_argument('--max-calls', type=int, required=True)
    x = sub.add_parser('scope-diagnose'); x.add_argument('dataset'); x.add_argument('--corpus', required=True); x.add_argument('--run', required=True); x.add_argument('--out', required=True); x.add_argument('--legacy', action='store_true')
    x = sub.add_parser('scope-adjudicate'); x.add_argument('dataset'); x.add_argument('--corpus', required=True); x.add_argument('--run', required=True); x.add_argument('--decisions', required=True); x.add_argument('--out', required=True)
    x = sub.add_parser('scope-compare'); x.add_argument('left'); x.add_argument('right'); x.add_argument('--out', required=True)
    x = sub.add_parser('scope-pair-packet'); x.add_argument('dataset'); x.add_argument('--corpus', required=True); x.add_argument('--left', required=True); x.add_argument('--right', required=True); x.add_argument('--seed', type=int, required=True); x.add_argument('--out', required=True)
    x = sub.add_parser('scope-experiment'); x.add_argument('dataset'); x.add_argument('--corpus', required=True); x.add_argument('--out', required=True); x.add_argument('--split', choices=['dev','test'], default='dev'); x.add_argument('--seed', type=int, required=True); x.add_argument('--max-calls', type=int, required=True); x.add_argument('--resume', action='store_true'); x.add_argument('--judge', action='store_true'); x.add_argument('--prices')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def results(path):
    rows = [json.loads(s) for s in (Path(path) / 'cases.jsonl').read_text(encoding='utf-8-sig').splitlines() if s.strip()]
    return {r['id']: r for r in rows}


def checked_run(path, cases, bundle):
    manifest = read(Path(path) / 'manifest.json')
    selected = [c for c in cases if c.split == manifest['split']]
    if manifest['scope_version'] != bundle['scope_version'] or manifest['dataset_hash'] != sha([c.model_dump() for c in selected]):
        raise ValueError('题目/语料与答案运行清单不一致')
    rows = results(path)
    if not set(rows) <= {c.id for c in selected}:
        raise ValueError('答案包含运行范围之外的题目')
    return selected, manifest, rows


async def execute(args):
    c = args.command
    if c == 'scope-export':
        from ..config import Settings
        from ..store import Store
        from ..access import AuthorizedStore
        from .scope_store import export_scope
        settings = Settings()
        revision = (await Store(settings).rows('SELECT revision FROM authorization_state WHERE id=1'))[0]['revision']
        bundle = await export_scope(AuthorizedStore(settings, args.user, revision))
        if Path(args.out).exists():
            raise ValueError('导出文件已存在，使用新路径')
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        write_json(args.out, bundle)
        print(json.dumps({'scope_version': bundle['scope_version'], 'members': len(bundle['members'])}))
        return
    if c == 'scope-compare':
        print(json.dumps(paired_compare(args.left, args.right, args.out), ensure_ascii=False))
        return
    if c == 'scope-diagnose' and args.legacy:
        cases, corpus = load(args.dataset), read(args.corpus)
        rows = results(args.run)
        data = {case.id: diagnose(case, rows[case.id], corpus) for case in cases if case.id in rows}
        write_json(args.out, data)
        print(f'诊断 {len(data)} 题；缺失阶段保留 null，不改写原运行。')
        return
    cases = load_scope(args.dataset)
    if c == 'scope-review':
        packet = read(args.packet)
        if sha([c.model_dump() for c in cases]) != sha(packet['cases']):
            raise ValueError('题目与已审核包不一致，需重新审核')
        reviewed = review_scope(cases, args.ids.split(','), args.reviewer, args.confirmation)
        for case in reviewed:
            if case.id in args.ids.split(','):
                case.review_record['packet_hash'] = sha(packet)
        save(args.out, [c.model_dump() for c in reviewed], exclusive=True)
        return
    bundle = read(args.corpus)
    validate_scope(cases, bundle)
    if c == 'scope-validate':
        print(f'跨库校验通过：{len(cases)} 题')
    elif c == 'scope-publish':
        print(json.dumps(publish_scope(cases, bundle, args.directory, args.version), ensure_ascii=False))
    elif c == 'scope-packet':
        rows = checked_run(args.run, cases, bundle)[2] if args.run else None
        print(json.dumps(review_packet(cases, bundle, args.out, rows), ensure_ascii=False))
    elif c == 'scope-diagnose':
        selected, _, rows = checked_run(args.run, cases, bundle)
        write_json(args.out, {case.id: diagnose(case, rows[case.id], bundle['corpus']) for case in selected if case.id in rows})
    elif c == 'scope-pair-packet':
        selected, _, _ = checked_run(args.left, cases, bundle)
        checked_run(args.right, cases, bundle)
        print(json.dumps(paired_packet(selected, bundle, args.left, args.right, args.out, args.seed), ensure_ascii=False))
    elif c == 'scope-adjudicate':
        selected, manifest, rows = checked_run(args.run, cases, bundle)
        decisions = [json.loads(s) for s in Path(args.decisions).read_text(encoding='utf-8-sig').splitlines() if s.strip()]
        if len({d['id'] for d in decisions}) != len(decisions):
            raise ValueError('人工裁决 ID 重复')
        by_id = {case.id: case for case in selected}
        for d in decisions:
            if d.get('id') not in rows or type(d.get('correct')) is not bool or not all(d.get(k) for k in ('reviewer', 'reason')):
                raise ValueError('人工裁决需有效 ID、布尔值、审核者与原因')
            row = rows[d['id']]
            expected = {'answer': 'completed', 'refuse': 'refused', 'clarify': 'clarify'}[row['expected_behavior']]
            if d['correct'] and row['status'] != expected:
                raise ValueError('执行错误或期望行为不匹配不能标为通过')
            row['human'] = d
            row['diagnosis'] = diagnose(by_id[d['id']], row, bundle['corpus'])
        root = Path(args.out); root.mkdir(parents=True, exist_ok=False)
        write_json(root / 'manifest.json', manifest)
        if (Path(args.run) / 'requests.jsonl').exists():
            import shutil
            shutil.copyfile(Path(args.run) / 'requests.jsonl', root / 'requests.jsonl')
        save(root / 'cases.jsonl', list(rows.values()), exclusive=True)
        print(json.dumps(scope_report(root, selected, list(rows.values()), manifest['prices']), ensure_ascii=False))
    elif c in ('scope-run', 'scope-experiment'):
        from ..config import Settings
        from ..providers import Models
        from .scope_runner import run_scope, run_pair_scope
        if args.max_calls < 1:
            raise ValueError('真实模型调用预算需为正整数')
        settings = Settings(); models = Models(settings); models.max_calls = args.max_calls
        try:
            if c == 'scope-experiment':
                data = await run_pair_scope(settings, models, args.dataset, args.corpus, args.out,
                    split=args.split, seed=args.seed, max_calls=args.max_calls, resume=args.resume,
                    judge_answers=args.judge, prices=read(args.prices) if args.prices else None)
            else:
                data = await run_scope(settings, models, args.dataset, args.corpus, args.out,
                    split=args.split, experiment=args.experiment, resume=args.resume, judge_answers=args.judge,
                    prices=read(args.prices) if args.prices else None)
            print(json.dumps(data, ensure_ascii=False))
        finally:
            await models.close()
