import hashlib
import json
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field, model_validator

class Anchor(BaseModel):
    source_version: str
    locator: dict
    quote: str = Field(min_length=1)

class Case(BaseModel):
    id: str
    case_revision: int = Field(default=1, ge=1)
    status: Literal['draft','review','active','retired'] = 'draft'
    split: Literal['dev','test']
    theme: str
    tags: list[str] = Field(default_factory=list)
    knowledge_base: str
    corpus_version: str
    question: str = Field(min_length=1)
    history: list[dict[str,str]] = Field(default_factory=list)
    expected_behavior: Literal['answer','refuse','clarify']
    reference_answer: str = ''
    required_facts: list[str] = Field(default_factory=list)
    forbidden_facts: list[str] = Field(default_factory=list)
    evidence_groups: list[list[Anchor]] = Field(default_factory=list)
    change_reason: str = ''
    review_record: dict = Field(default_factory=dict)

    @model_validator(mode='after')
    def check_answer(self):
        if self.expected_behavior == 'answer' and (not self.required_facts or not self.evidence_groups):
            raise ValueError('可回答样本需有必要事实和原文证据组')
        if any(not g for g in self.evidence_groups):
            raise ValueError('证据组不能为空')
        if self.status == 'active' and (not self.review_record.get('reviewer') or not self.review_record.get('approved_at')):
            raise ValueError('有效样本必须完成人工审核')
        return self

def sha(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',',':')).encode()).hexdigest()

def load(path):
    result = [Case.model_validate(json.loads(line)) for line in Path(path).read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    if len({c.id for c in result}) != len(result):
        raise ValueError('样本 ID 重复')
    splits = {}
    for c in result:
        if c.status == 'retired':
            continue
        splits.setdefault(c.theme, set()).add(c.split)
    if any(len(s) > 1 for s in splits.values()):
        raise ValueError('同一事实主题不能横跨 dev/test')
    return result

def save(path, cases, exclusive=False):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x' if exclusive else 'w', encoding='utf-8') as f:
        for case in cases:
            f.write(json.dumps(case.model_dump() if isinstance(case, Case) else case, ensure_ascii=False) + '\n')

def same_locator(a, b):
    keys = ('page','paragraph','section')
    return all(a.get(k) == b.get(k) for k in keys)

def anchor_interval(anchor, corpus):
    for p in corpus['paragraphs']:
        if p['version_id'] == anchor.source_version and same_locator(p['locator'], anchor.locator):
            text, quote = p['text'], anchor.quote
            offset = anchor.locator.get('start')
            if offset is None:
                if text.count(quote) != 1:
                    raise ValueError('节选不唯一，请提供原文 start 字符位置')
                offset = text.find(quote)
            if text[offset:offset+len(quote)] != quote:
                raise ValueError('证据节选与原文不一致')
            return offset, offset+len(quote)
    raise ValueError('证据文档版本或段落不存在')

def validate(cases, corpus, published=False):
    for case in cases:
        if case.status == 'retired':
            if published and (not case.review_record.get('reviewer') or not case.review_record.get('approved_at')):
                raise ValueError(f'{case.id}: 停用样本也需人工审核')
            continue
        if case.corpus_version != corpus['corpus_version'] or case.knowledge_base != corpus['knowledge_base']:
            raise ValueError(f'{case.id}: 数据集与知识快照不匹配')
        if published and case.status != 'active':
            raise ValueError(f'{case.id}: 发布前需人工审核并设为 active')
        for group in case.evidence_groups:
            for anchor in group:
                anchor_interval(anchor, corpus)

def publish(draft, corpus, directory, version):
    cases = load(draft)
    validate(cases, corpus, published=True)
    root = Path(directory) / version
    root.mkdir(parents=True, exist_ok=False)
    save(root / 'cases.jsonl', cases, exclusive=True)
    manifest = {'dataset_version': version, 'corpus_version': corpus['corpus_version'],
                'corpus_hash': sha(corpus), 'dataset_hash': sha([c.model_dump() for c in cases]), 'case_count': len(cases)}
    (root/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest
