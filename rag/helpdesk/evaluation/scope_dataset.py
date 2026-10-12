"""跨库题与授权语料独立冻结，旧单库发布格式保持不变。"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .dataset import Anchor, anchor_interval, sha, same_locator

SCHEMA = 'scope-eval-v1'


class ScopeCase(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1)
    case_revision: int = Field(default=1, ge=1)
    status: Literal['draft', 'review', 'active', 'retired'] = 'review'
    split: Literal['dev', 'test']
    theme: str = Field(min_length=1)
    fact_topics: list[str] = Field(default_factory=list)
    old_topic_overlap: list[str] = Field(default_factory=list)
    scene: Literal['comparison', 'ambiguity', 'multi-turn', 'conflict-conditions', 'no-answer', 'control']
    scope_version: str
    scope_mode: Literal['default', 'selected'] = 'default'
    kb_ids: list[str] = Field(default_factory=list, max_length=32)
    expected_route_kb_ids: list[str] | None = None
    expected_required_kb_ids: list[str] = Field(default_factory=list, max_length=5)
    question: str = Field(min_length=1, max_length=12000)
    history: list[dict[str, str]] = Field(default_factory=list)
    history_mode: Literal['none', 'fixed'] = 'none'
    expected_behavior: Literal['answer', 'refuse', 'clarify']
    reference_answer: str = ''
    required_facts: list[str] = Field(default_factory=list)
    forbidden_facts: list[str] = Field(default_factory=list)
    evidence_groups: list[list[Anchor]] = Field(default_factory=list)
    subtasks: list[dict[str, str]] = Field(default_factory=list)
    review_record: dict = Field(default_factory=dict)

    @model_validator(mode='after')
    def coherent(self):
        if (self.scope_mode == 'selected') != bool(self.kb_ids):
            raise ValueError('指定范围需 kb_ids，默认范围不能携带 kb_ids')
        if len(set(self.kb_ids)) != len(self.kb_ids):
            raise ValueError('知识库 ID 重复')
        if self.expected_behavior == 'answer' and (not self.required_facts or not self.evidence_groups):
            raise ValueError('可回答题需必要事实和原文证据')
        if any(not g for g in self.evidence_groups):
            raise ValueError('证据组不能为空')
        if bool(self.history) != (self.history_mode == 'fixed'):
            raise ValueError('固定历史必须标记 fixed；真实会话链使用独立 Web 联测')
        if any(h.get('role') not in ('user', 'assistant') or not h.get('content') for h in self.history):
            raise ValueError('历史仅允许 user/assistant 正文')
        if self.status == 'active' and not all(self.review_record.get(k) for k in ('reviewer', 'approved_at', 'confirmation')):
            raise ValueError('冻结题需独立的人工确认记录')
        return self


def load_scope(path):
    cases = [ScopeCase.model_validate(json.loads(s)) for s in Path(path).read_text(encoding='utf-8-sig').splitlines() if s.strip()]
    if len({c.id for c in cases}) != len(cases):
        raise ValueError('样本 ID 重复')
    topics = {}
    for c in cases:
        if c.status == 'retired':
            continue
        for topic in [c.theme, *c.fact_topics]:
            topics.setdefault(topic, set()).add(c.split)
    if any(len(s) > 1 for s in topics.values()):
        raise ValueError('事实主题不能跨开发和正式测试划分')
    return cases


def paragraphs_from_chunks(chunks):
    paragraphs = {}
    for c in sorted(chunks, key=lambda c: (c['version_id'], c['ordinal'])):
        loc = c['locator']
        key = (c['version_id'], loc.get('page'), loc.get('paragraph'), loc.get('section'))
        p = paragraphs.setdefault(key, {'version_id': c['version_id'], 'kb_id': c['kb_id'],
            'locator': {k: v for k, v in loc.items() if k not in ('start', 'end')}, 'text': ''})
        start, end = loc['start'], loc['end']
        if start < 0 or end != start + len(c['text']) or start > len(p['text']):
            raise ValueError('切片区间无效或存在缺口')
        overlap = max(0, len(p['text']) - start)
        if p['text'][start:start + overlap] != c['text'][:overlap]:
            raise ValueError('重叠原文不一致')
        p['text'] += c['text'][overlap:]
    return list(paragraphs.values())


def seal_bundle(members, principal, chunks):
    bundle = {'schema': SCHEMA, 'members': sorted(members, key=lambda m: m['kb_id']),
              'principal': principal, 'corpus': {'chunks': chunks, 'paragraphs': paragraphs_from_chunks(chunks)}}
    bundle['scope_version'] = sha(bundle)
    validate_bundle(bundle)
    return bundle


def validate_bundle(bundle):
    if bundle.get('schema') != SCHEMA or bundle.get('scope_version') != sha({k: v for k, v in bundle.items() if k != 'scope_version'}):
        raise ValueError('冻结语料格式或校验值无效')
    principal = bundle['principal']
    if not principal.get('user_id') or type(principal.get('revision')) is not int or principal['revision'] < 0:
        raise ValueError('缺少冻结主体与授权版本')
    members = bundle['members']
    if not members or len({m['kb_id'] for m in members}) != len(members):
        raise ValueError('冻结成员为空或重复')
    versions = {}
    for m in members:
        if not m['snapshot_id'] or type(m['default_search']) is not bool or len(set(m['version_ids'])) != len(m['version_ids']):
            raise ValueError('快照成员配置无效')
        for v in m['version_ids']:
            if v in versions:
                raise ValueError('文档版本不能跨库')
            versions[v] = m
    chunks = bundle['corpus']['chunks']
    if len({c['id'] for c in chunks}) != len(chunks):
        raise ValueError('切片 ID 重复')
    for c in chunks:
        member = versions.get(c['version_id'])
        if not member or c['kb_id'] != member['kb_id'] or c['knowledge_base_name'] != member['name'] or c['organization'] != member['organization']:
            raise ValueError('切片越出冻结授权范围或组织元数据不一致')
    if paragraphs_from_chunks(chunks) != bundle['corpus']['paragraphs']:
        raise ValueError('原文与冻结切片不一致')


def validate_scope(cases, bundle, *, published=False):
    validate_bundle(bundle)
    members = {m['kb_id']: m for m in bundle['members']}
    for c in cases:
        if c.scope_version != bundle['scope_version']:
            raise ValueError(f'{c.id}: 题目与授权语料版本不匹配')
        if published and c.status != 'active':
            raise ValueError(f'{c.id}: 正式运行只允许已审核有效题')
        allowed = set(c.kb_ids) if c.scope_mode == 'selected' else {m['kb_id'] for m in members.values() if m['default_search']}
        if not allowed <= members.keys():
            raise ValueError(f'{c.id}: 指定范围越出冻结成员')
        if not set(c.expected_required_kb_ids) <= allowed or (c.expected_route_kb_ids is not None and not set(c.expected_route_kb_ids) <= allowed):
            raise ValueError(f'{c.id}: 预期路由越出请求范围')
        versions = {v for k in allowed for v in members[k]['version_ids']}
        for group in c.evidence_groups:
            for anchor in group:
                if anchor.source_version not in versions:
                    raise ValueError(f'{c.id}: 黄金证据在请求授权范围之外')
                anchor_interval(anchor, bundle['corpus'])


def review_scope(cases, ids, reviewer, confirmation):
    if not reviewer.strip() or not confirmation.strip() or not set(ids) <= {c.id for c in cases}:
        raise ValueError('需有效题 ID、审核者和实际确认原话')
    record = {'reviewer': reviewer, 'confirmation': confirmation, 'approved_at': datetime.now(timezone.utc).isoformat()}
    return [ScopeCase.model_validate({**c.model_dump(), 'status': 'active', 'review_record': record}) if c.id in ids else c for c in cases]


def publish_scope(cases, bundle, directory, version):
    validate_scope(cases, bundle, published=True)
    root = Path(directory) / version
    root.mkdir(parents=True, exist_ok=False)
    from .dataset import save
    save(root / 'cases.jsonl', [c.model_dump() for c in cases], exclusive=True)
    (root / 'corpus.json').write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding='utf-8')
    manifest = {'schema': SCHEMA, 'version': version, 'dataset_hash': sha([c.model_dump() for c in cases]),
                'scope_version': bundle['scope_version'], 'counts': {s: sum(c.split == s for c in cases) for s in ('dev', 'test')}}
    (root / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest
