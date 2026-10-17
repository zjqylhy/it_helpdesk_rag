"""只读转换已审核发布集，保留默认范围、出处坐标和人工来源记录。"""

import hashlib
import json
from pathlib import Path

from helpdesk.application.evaluation import Anchor, EvalCase, covered, load_cases, write_new
from helpdesk.application.experiment_proposal import proposal
from helpdesk.domain.models import Row


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def read_release(source: Path) -> tuple[list[Row], Row, Row]:
    cases = [
        json.loads(line)
        for line in (source / "cases.jsonl").read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    bundle = json.loads((source / "corpus.json").read_text(encoding="utf-8-sig"))
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8-sig"))
    approval = json.loads((source / "approval.json").read_text(encoding="utf-8-sig"))
    if (
        manifest.get("schema") != "scope-eval-v1"
        or bundle.get("schema") != "scope-eval-v1"
        or manifest["dataset_hash"] != canonical_hash(cases)
        or manifest["scope_version"] != bundle["scope_version"]
        or bundle["scope_version"]
        != canonical_hash({k: v for k, v in bundle.items() if k != "scope_version"})
        or approval.get("published_dataset_hash") != manifest["dataset_hash"]
        or set(approval.get("approved_case_ids", [])) != {c["id"] for c in cases}
        or len({c["id"] for c in cases}) != len(cases)
    ):
        raise ValueError("原发布集、范围或审批清单校验不一致")
    members = {m["kb_id"]: m for m in bundle["members"]}
    if len(members) != len(bundle["members"]):
        raise ValueError("冻结知识库重复")
    versions = {v: m["kb_id"] for m in members.values() for v in m["version_ids"]}
    if len(versions) != sum(len(m["version_ids"]) for m in members.values()):
        raise ValueError("版本跨库或重复")
    if len({c["id"] for c in bundle["corpus"]["chunks"]}) != len(bundle["corpus"]["chunks"]):
        raise ValueError("切片编号重复")
    for chunk in bundle["corpus"]["chunks"]:
        if (
            versions.get(chunk["version_id"]) != chunk["kb_id"]
            or hashlib.sha256(chunk["text"].encode()).hexdigest() != chunk["content_hash"]
            or chunk["locator"]["end"] - chunk["locator"]["start"] != len(chunk["text"])
        ):
            raise ValueError("原切片文本、坐标或范围校验失败")
    for case in cases:
        review = case.get("review_record", {})
        if (
            case.get("status") != "active"
            or case["scope_version"] != bundle["scope_version"]
            or not all(review.get(k) for k in ("reviewer", "approved_at", "confirmation", "packet_hash"))
            or review["packet_hash"] != approval["packet_hash"]
        ):
            raise ValueError("仅转换已审核且绑定当前发布集的题目")
    if approval.get("approved_fixture_ids"):
        fixtures = json.loads((source / "permission-fixtures.json").read_text(encoding="utf-8"))
        if (
            canonical_hash(fixtures) != approval.get("approved_fixture_hash")
            or {item["id"] for item in fixtures["cases"]} != set(approval["approved_fixture_ids"])
            or fixtures.get("human_review_status") != "approved"
        ):
            raise ValueError("权限协议与审批记录不一致")
    return cases, bundle, manifest


def normalize_case(case: Row, bundle: Row, manifest: Row) -> EvalCase:
    allowed = (
        set(case["kb_ids"])
        if case["scope_mode"] == "selected"
        else {m["kb_id"] for m in bundle["members"] if m["default_search"]}
    )
    members = {m["kb_id"]: m for m in bundle["members"]}
    if not allowed.issubset(members):
        raise ValueError("题目范围越出冻结成员")
    versions = {v for key in allowed for v in members[key]["version_ids"]}
    groups = []
    for group in case["evidence_groups"]:
        anchors = []
        for anchor in group:
            loc = anchor["locator"]
            version = anchor["source_version"]
            quote = anchor["quote"]
            matches = [
                p
                for p in bundle["corpus"]["paragraphs"]
                if p["version_id"] == version
                and all(p["locator"].get(key) == loc.get(key) for key in ("paragraph", "page", "section"))
            ]
            if len(matches) != 1 or version not in versions:
                raise ValueError("黄金出处不唯一或越出请求范围")
            text = matches[0]["text"]
            start = loc.get("start")
            if start is None:
                if text.count(quote) != 1:
                    raise ValueError("节选不唯一，必须保留明确字符位置")
                start = text.index(quote)
            if type(start) is not int or start < 0 or text[start : start + len(quote)] != quote:
                raise ValueError("黄金节选与原文不一致")
            normalized = Anchor(
                version_id=version,
                paragraph=loc["paragraph"],
                page=loc.get("page"),
                section=loc.get("section"),
                start=start,
                end=start + len(quote),
                quote=quote,
            )
            if not covered(normalized, bundle["corpus"]["chunks"]):
                raise ValueError("切片并集无法覆盖黄金区间")
            anchors.append(normalized)
        groups.append(anchors)
    review = case["review_record"]
    return EvalCase(
        id=case["id"],
        question=case["question"],
        kb_ids=case["kb_ids"],
        scope_mode=case["scope_mode"],
        history=case["history"],
        expected_behavior=case["expected_behavior"],
        evidence_groups=groups,
        required_facts=case["required_facts"],
        forbidden_facts=case["forbidden_facts"],
        theme=case["theme"],
        fact_topics=case["fact_topics"],
        scene=case["scene"],
        split=case["split"],
        reviewer=review["reviewer"],
        reviewed_at=review["approved_at"],
        expected_route_kb_ids=case["expected_route_kb_ids"],
        expected_required_kb_ids=case["expected_required_kb_ids"],
        source={
            "release": manifest["version"],
            "dataset_hash": manifest["dataset_hash"],
            "scope_version": bundle["scope_version"],
            "case_hash": canonical_hash(case),
            "case_revision": case["case_revision"],
            "review_record": review,
            "old_topic_overlap": case["old_topic_overlap"],
            "subtasks": case["subtasks"],
            "reference_answer": case["reference_answer"],
        },
    )


def prepare_release(source: Path, out: Path) -> Row:
    cases, bundle, manifest = read_release(source)
    converted = [normalize_case(case, bundle, manifest) for case in cases]
    # 先验证全集的主题隔离；新目录写入失败也不能覆盖原发布记录。
    topics: dict[str, str] = {}
    for case in converted:
        for topic in [case.theme, *case.fact_topics]:
            if topics.setdefault(topic, case.split) != case.split:
                raise ValueError("事实主题跨 dev/test")
    out.mkdir(parents=True, exist_ok=False)
    for split in ("all", "dev", "test"):
        rows = [c for c in converted if split == "all" or c.split == split]
        with (out / (split + ".jsonl")).open("x", encoding="utf-8", newline="\n") as stream:
            for case in rows:
                stream.write(json.dumps(case.model_dump(), ensure_ascii=False) + "\n")
        if rows:
            load_cases(out / (split + ".jsonl"))
    write_new(out / "source-corpus.json", bundle)
    if (source / "permission-fixtures.json").exists():
        write_new(
            out / "source-permissions.json",
            json.loads((source / "permission-fixtures.json").read_text(encoding="utf-8")),
        )
    report = {
        "schema": "rewrite-scope-preparation-v1",
        "source_manifest": manifest,
        "source_file_hashes": {
            name: hashlib.sha256((source / name).read_bytes()).hexdigest()
            for name in ("cases.jsonl", "corpus.json", "manifest.json", "approval.json")
        },
        "normalized_hash": canonical_hash([c.model_dump() for c in converted]),
        "cases": len(converted),
        "splits": {s: sum(c.split == s for c in converted) for s in ("dev", "test")},
        "chunks": len(bundle["corpus"]["chunks"]),
        "required_groups": sum(len(c.evidence_groups) for c in converted),
        "scope_modes": {s: sum(c.scope_mode == s for c in converted) for s in ("default", "selected")},
        "model_calls": 0,
        "approval": "source_review_preserved_not_execution_authorization",
        "remaining_gates": [
            "live_authorization_and_corpus",
            "model_routes_and_ca",
            "token_estimator_feasibility",
            "fee_budget_approval",
            "actual_answers_human_adjudication",
        ],
    }
    write_new(out / "preparation.json", report)
    write_new(out / "experiment-proposal.json", proposal(converted, bundle))
    return {
        key: report[key]
        for key in (
            "schema",
            "cases",
            "splits",
            "chunks",
            "required_groups",
            "scope_modes",
            "model_calls",
            "remaining_gates",
        )
    }
