"""重新绑定只改变来源标识；原事实审核与新来源审批分别留档。"""

import copy
import hashlib
import json
from pathlib import Path

from helpdesk.application.evaluation import EvalCase, covered, load_cases, write_new
from helpdesk.application.experiment_proposal import proposal
from helpdesk.application.releases import canonical_hash, normalize_case, read_release
from helpdesk.domain.models import Row
from helpdesk.domain.rag import BoundScope

MATCH_FIELDS = (
    "ordinal",
    "text",
    "locator",
    "content_hash",
    "title",
    "filename",
    "source",
    "knowledge_base_name",
    "organization",
    "embedding_model",
    "dimension",
)


def signature(chunk: Row) -> str:
    return canonical_hash({key: chunk[key] for key in MATCH_FIELDS})


def unique_mapping(source: Row, scope: BoundScope, rows: list[Row]) -> Row:
    members = source["members"]
    if len(scope.kb_ids) != len(members) or len(rows) != len(source["corpus"]["chunks"]):
        raise ValueError("服务器可见范围与原审核来源数量不同")
    kb_map: dict[str, str] = {}
    for member in members:
        matches = [
            k
            for k in scope.knowledge_bases
            if (k["name"], k["organization"]) == (member["name"], member["organization"])
        ]
        if len(matches) != 1 or matches[0]["default_search"] != member["default_search"]:
            raise ValueError("知识库不唯一或默认范围发生变化")
        kb_map[member["kb_id"]] = matches[0]["id"]
    if len(set(kb_map.values())) != len(kb_map):
        raise ValueError("多个来源知识库合并到同一目标")
    index: dict[str, list[Row]] = {}
    for row in rows:
        index.setdefault(signature(row), []).append(row)
    chunk_map: dict[str, str] = {}
    version_map: dict[str, str] = {}
    documents: set[str] = set()
    for chunk in source["corpus"]["chunks"]:
        matches = index.get(signature(chunk), [])
        if len(matches) != 1 or matches[0]["kb_id"] != kb_map[chunk["kb_id"]]:
            raise ValueError("原文映射缺失、歧义或跨库")
        target = matches[0]
        if version_map.setdefault(chunk["version_id"], target["version_id"]) != target["version_id"]:
            raise ValueError("同一来源版本映射到多个目标版本")
        chunk_map[chunk["id"]] = target["id"]
        documents.add(target["document_id"])
    source_versions = {v for m in members for v in m["version_ids"]}
    if (
        set(version_map) != source_versions
        or set(version_map.values()) != set(scope.version_ids)
        or len(set(version_map.values())) != len(version_map)
        or len(set(chunk_map.values())) != len(chunk_map)
        or set(chunk_map.values()) != {r["id"] for r in rows}
    ):
        raise ValueError("版本或切片不是完整的一一映射")
    snapshots = {s["kb_id"]: s for s in scope.snapshots}
    for member in members:
        target_kb_id = kb_map[member["kb_id"]]
        if target_kb_id not in snapshots or set(snapshots[target_kb_id]["version_ids"]) != {
            version_map[v] for v in member["version_ids"]
        }:
            raise ValueError("活动快照与来源版本不一致")
    return {
        "kb_ids": kb_map,
        "version_ids": version_map,
        "chunk_ids": chunk_map,
        "document_ids": sorted(documents),
    }


def rebound(source: Row, scope: BoundScope, rows: list[Row], principal: Row, mapping: Row) -> Row:
    result = copy.deepcopy(source)
    result.pop("scope_version")
    result["principal"] = principal
    snapshots = {s["kb_id"]: s for s in scope.snapshots}
    live = {r["id"]: r for r in rows}
    for member in result["members"]:
        member["kb_id"] = mapping["kb_ids"][member["kb_id"]]
        member["version_ids"] = [mapping["version_ids"][v] for v in member["version_ids"]]
        member["snapshot_id"] = snapshots[member["kb_id"]]["id"]
        member["snapshot_version_hash"] = canonical_hash(snapshots[member["kb_id"]]["version_ids"])
    for chunk in result["corpus"]["chunks"]:
        chunk["id"] = mapping["chunk_ids"][chunk["id"]]
        chunk["version_id"] = mapping["version_ids"][chunk["version_id"]]
        chunk["kb_id"] = mapping["kb_ids"][chunk["kb_id"]]
        chunk["vector_hash"] = live[chunk["id"]]["vector_hash"]
    for paragraph in result["corpus"]["paragraphs"]:
        paragraph["version_id"] = mapping["version_ids"][paragraph["version_id"]]
        if "kb_id" in paragraph:
            paragraph["kb_id"] = mapping["kb_ids"][paragraph["kb_id"]]
    result["scope_version"] = canonical_hash(result)
    return result


def prepare_rebinding(source: Path, out: Path, scope: BoundScope, rows: list[Row], principal: Row) -> Row:
    cases, bundle, manifest = read_release(source)
    if principal["role"] != "user" or principal["revision"] != scope.revision:
        raise ValueError("必须绑定当前普通评测主体")
    mapping = unique_mapping(bundle, scope, rows)
    target = rebound(bundle, scope, rows, principal, mapping)
    converted = [normalize_case(c, bundle, manifest) for c in cases]
    normalized: list[EvalCase] = []
    for case in converted:
        data = case.model_dump()
        data["kb_ids"] = [mapping["kb_ids"][k] for k in case.kb_ids]
        for name in ("expected_route_kb_ids", "expected_required_kb_ids"):
            data[name] = [mapping["kb_ids"][k] for k in data[name]] if data[name] is not None else None
        for group in data["evidence_groups"]:
            for anchor in group:
                anchor["version_id"] = mapping["version_ids"][anchor["version_id"]]
        data["source"] = {
            **data["source"],
            "scope_version": target["scope_version"],
            "rebinding": {
                "status": "pending_review",
                "original_scope_version": bundle["scope_version"],
                "mapping_hash": canonical_hash(mapping),
            },
        }
        item = EvalCase.model_validate(data)
        if any(not covered(a, target["corpus"]["chunks"]) for g in item.evidence_groups for a in g):
            raise ValueError("转换后的黄金出处不完整")
        normalized.append(item)
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    for split in ("all", "dev", "test"):
        items = [c for c in normalized if split == "all" or c.split == split]
        with (out / (split + ".jsonl")).open("x", encoding="utf-8", newline="\n") as stream:
            for item in items:
                stream.write(json.dumps(item.model_dump(), ensure_ascii=False) + "\n")
        if items:
            load_cases(out / (split + ".jsonl"))
    write_new(out / "source-corpus.json", target)
    write_new(out / "mapping.json", mapping)
    write_new(out / "experiment-proposal.json", proposal(normalized, target))
    packet = {
        "schema": "rewrite-rebind-review-v1",
        "status": "pending_review",
        "original_scope_version": bundle["scope_version"],
        "scope_version": target["scope_version"],
        "original_dataset_hash": manifest["dataset_hash"],
        "subject": principal,
        "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.iterdir())},
        "cases": len(normalized),
        "chunks": len(rows),
        "model_calls": 0,
        "original_approval_transferred": False,
    }
    packet["packet_hash"] = canonical_hash(packet)
    write_new(out / "review-packet.json", packet)
    return packet


def verify_rebinding_approval(cases: Path, corpus: Path | None, approval: Path | None) -> str | None:
    """只认可明确绑定整个审核包的新审批；在创建实验目录前执行。"""
    loaded = load_cases(cases)
    folder = cases.resolve().parent
    packet_path = folder / "review-packet.json"
    if not packet_path.exists() and not any(c.source.get("rebinding") for c in loaded):
        return None
    if approval is None:
        raise ValueError("服务器来源重新绑定尚未审批，必须提供 --rebind-approval")
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    digest = packet.pop("packet_hash")
    if canonical_hash(packet) != digest or packet["schema"] != "rewrite-rebind-review-v1":
        raise ValueError("来源审核包哈希无效")
    if (
        cases.name not in packet["files"]
        or corpus is None
        or corpus.resolve() != folder / "source-corpus.json"
    ):
        raise ValueError("题目或语料不属于来源审核包")
    for name, expected in packet["files"].items():
        if Path(name).name != name or hashlib.sha256((folder / name).read_bytes()).hexdigest() != expected:
            raise ValueError("来源审核包文件发生变化")
    decision = json.loads(approval.read_text(encoding="utf-8"))
    if (
        decision.get("schema") != "rewrite-rebind-approval-v1"
        or decision.get("confirmed") is not True
        or decision.get("packet_hash") != digest
        or not decision.get("reviewer")
        or not decision.get("reviewed_at")
    ):
        raise ValueError("新来源审批未确认或未绑定当前审核包")
    if any(c.source.get("scope_version") != packet["scope_version"] for c in loaded):
        raise ValueError("题目来源与审核包不一致")
    return hashlib.sha256(approval.read_bytes()).hexdigest()


def verify_frozen_rebinding(folder: Path, approval_hash: str) -> None:
    raw = (folder / "rebind-approval.json").read_bytes()
    packet = json.loads((folder / "rebind-review-packet.json").read_text(encoding="utf-8"))
    digest = packet.pop("packet_hash")
    decision = json.loads(raw)
    if (
        hashlib.sha256(raw).hexdigest() != approval_hash
        or canonical_hash(packet) != digest
        or decision.get("packet_hash") != digest
        or decision.get("confirmed") is not True
    ):
        raise ValueError("冻结的新来源审批或审核包发生变化")
