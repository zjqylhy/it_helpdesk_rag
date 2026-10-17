import argparse
import copy
import hashlib
import json
from dataclasses import replace

import httpx
import psycopg
import pytest
from test_m3_preparation import source_release
from test_models_and_limits import settings

from helpdesk.application.rebinding import (
    prepare_rebinding,
    unique_mapping,
    verify_frozen_rebinding,
    verify_rebinding_approval,
)
from helpdesk.application.releases import canonical_hash
from helpdesk.domain.rag import BoundScope
from helpdesk.entrypoints import rebind as rebind_cli
from helpdesk.entrypoints.evaluate import freeze, verify_source
from helpdesk.settings import Settings


@pytest.fixture
def rebinding(tmp_path):
    source = tmp_path / "source"
    case, bundle = source_release(source)
    bundle["corpus"]["chunks"][0].update(
        ordinal=0, filename="synthetic.txt", source="synthetic", embedding_model="test", dimension=3
    )
    bundle["scope_version"] = canonical_hash({k: v for k, v in bundle.items() if k != "scope_version"})
    case["scope_version"] = bundle["scope_version"]
    manifest = json.loads((source / "manifest.json").read_text())
    manifest.update(dataset_hash=canonical_hash([case]), scope_version=bundle["scope_version"])
    approval = json.loads((source / "approval.json").read_text())
    approval["published_dataset_hash"] = manifest["dataset_hash"]
    for name, value in [("corpus.json", bundle), ("manifest.json", manifest), ("approval.json", approval)]:
        (source / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    (source / "cases.jsonl").write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    scope = BoundScope(
        ("new-kb",),
        ({"id": "new-kb", "name": "合成库", "organization": "合成学校", "default_search": True},),
        ("new-version",),
        ({"id": "new-snapshot", "kb_id": "new-kb", "version_ids": ["new-version"]},),
        8,
        "new-bound-scope",
    )
    rows = [
        {
            **bundle["corpus"]["chunks"][0],
            "id": "new-chunk",
            "kb_id": "new-kb",
            "version_id": "new-version",
            "document_id": "new-document",
            "vector_hash": "synthetic-vector-hash",
            "kb_name": "合成库",
        }
    ]
    principal = {"user_id": "new-user", "revision": 8, "role": "user", "group_ids": ["new-group"]}
    return source, scope, rows, principal


def approved(tmp_path, packet):
    path = tmp_path / "new-approval.json"
    path.write_text(
        json.dumps(
            {
                "schema": "rewrite-rebind-approval-v1",
                "confirmed": True,
                "packet_hash": packet["packet_hash"],
                "reviewer": "synthetic-human",
                "reviewed_at": "2026-10-17T10:00:00+08:00",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_rebinding_preserves_facts_review_and_default_scope(rebinding, tmp_path):
    source, scope, rows, principal = rebinding
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    out = tmp_path / "packet"
    packet = prepare_rebinding(source, out, scope, rows, principal)
    original = json.loads((source / "cases.jsonl").read_text())
    case = json.loads((out / "all.jsonl").read_text())
    assert case["kb_ids"] == [] and case["scope_mode"] == "default"
    assert case["evidence_groups"][0][0]["version_id"] == "new-version"
    assert case["expected_route_kb_ids"] == ["new-kb"]
    for field in (
        "question",
        "history",
        "required_facts",
        "forbidden_facts",
        "split",
        "theme",
        "fact_topics",
    ):
        assert case[field] == original[field]
    assert case["source"]["review_record"] == original["review_record"]
    assert case["source"]["rebinding"]["status"] == "pending_review"
    assert packet["status"] == "pending_review" and not packet["original_approval_transferred"]
    assert before == {p.name: p.read_bytes() for p in source.iterdir()}
    new_bundle = json.loads((out / "source-corpus.json").read_text())
    from helpdesk.application.evaluation import load_cases

    verify_source(load_cases(out / "all.jsonl")[0], new_bundle, scope, rows, "new-user", 8)
    with pytest.raises(ValueError, match="原文"):
        verify_source(
            load_cases(out / "all.jsonl")[0],
            new_bundle,
            scope,
            [{**rows[0], "vector_hash": "changed-vector"}],
            "new-user",
            8,
        )
    with pytest.raises(FileExistsError):
        prepare_rebinding(source, out, scope, rows, principal)


@pytest.mark.parametrize("mutation", ["text", "model", "extra", "duplicate", "snapshot", "default"])
def test_rebinding_rejects_missing_ambiguous_or_changed_scope(rebinding, mutation):
    source, scope, rows, _ = rebinding
    bundle = json.loads((source / "corpus.json").read_text())
    rows = copy.deepcopy(rows)
    if mutation == "text":
        rows[0]["text"] += "changed"
    if mutation == "model":
        rows[0]["embedding_model"] = "different-model"
    if mutation == "extra":
        rows.append({**rows[0], "id": "extra"})
    if mutation == "duplicate":
        bundle["corpus"]["chunks"].append({**bundle["corpus"]["chunks"][0], "id": "dup"})
        rows.append({**rows[0], "id": "dup-target"})
    if mutation == "snapshot":
        scope.snapshots[0]["version_ids"] = ["other-version"]
    if mutation == "default":
        scope.knowledge_bases[0]["default_search"] = False
    with pytest.raises(ValueError):
        unique_mapping(bundle, scope, rows)


def test_missing_approval_and_packet_tampering_are_rejected(rebinding, tmp_path):
    out = tmp_path / "packet"
    packet = prepare_rebinding(*rebinding[:1], out, *rebinding[1:])
    with pytest.raises(ValueError, match="尚未审批"):
        verify_rebinding_approval(out / "dev.jsonl", out / "source-corpus.json", None)
    approval = approved(tmp_path, packet)
    assert (
        verify_rebinding_approval(out / "dev.jsonl", out / "source-corpus.json", approval)
        == hashlib.sha256(approval.read_bytes()).hexdigest()
    )
    path = out / "dev.jsonl"
    path.write_text(path.read_text().replace("如何配置", "问题改变"), encoding="utf-8")
    with pytest.raises(ValueError, match="文件发生变化"):
        verify_rebinding_approval(path, out / "source-corpus.json", approval)


@pytest.mark.parametrize("change", ["confirmed", "packet_hash", "reviewer"])
def test_approval_must_be_explicit_and_bound_to_this_packet(rebinding, tmp_path, change):
    out = tmp_path / "packet"
    packet = prepare_rebinding(rebinding[0], out, *rebinding[1:])
    path = approved(tmp_path, packet)
    value = json.loads(path.read_text())
    value[change] = False if change == "confirmed" else ""
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="审批未确认"):
        verify_rebinding_approval(out / "dev.jsonl", out / "source-corpus.json", path)


async def test_freeze_refuses_pending_source_before_database_or_output(rebinding, tmp_path, monkeypatch):
    out = tmp_path / "packet"
    prepare_rebinding(rebinding[0], out, *rebinding[1:])

    def forbidden(*_):
        raise AssertionError("审批缺失时不得连接数据库")

    monkeypatch.setattr("helpdesk.entrypoints.evaluate.dependencies", forbidden)
    args = argparse.Namespace(
        cases=out / "dev.jsonl", source_corpus=out / "source-corpus.json", out=tmp_path / "experiment"
    )
    with pytest.raises(ValueError, match="尚未审批"):
        await freeze(args, settings())
    assert not args.out.exists()


async def test_provision_real_api_contract_resume_and_preserved_grants(
    rebinding, tmp_path, monkeypatch, app, client, admin, document, database
):
    # 映射算法另测；此处用已验证的合成输入核对真实管理接口、恢复与幂等。
    source, scope, rows, _ = rebinding
    kb_id = document["kb_id"]
    scope = replace(
        scope,
        kb_ids=(kb_id,),
        knowledge_bases=({**scope.knowledge_bases[0], "id": kb_id},),
        snapshots=({**scope.snapshots[0], "kb_id": kb_id},),
    )
    rows[0].update(kb_id=kb_id, document_id=document["document_id"])

    async def collect(_, principal, revision):
        return replace(scope, revision=revision), rows

    await client.post("/api/v1/admin/groups", headers=admin, json={"name": "original-group"})
    existing = (await client.get("/api/v1/admin/groups", headers=admin)).json()[0]["id"]
    path = "/api/v1/admin/documents/" + document["document_id"] + "/access"
    await client.put(path, headers=admin, json={"visibility": "groups", "group_ids": [existing]})
    real_client = httpx.AsyncClient

    def api_client(**kwargs):
        return real_client(transport=httpx.ASGITransport(app=app), **kwargs)

    monkeypatch.setattr(rebind_cli, "collect", collect)
    monkeypatch.setattr(rebind_cli.httpx, "AsyncClient", api_client)
    monkeypatch.setenv("HD_EVAL_TOKEN", admin["Authorization"].removeprefix("Bearer "))
    args = argparse.Namespace(
        source=source,
        api_url="http://127.0.0.1:18080",
        apply=True,
        username="eval-fixture",
        group_name="eval-fixture-group",
        credentials=tmp_path / "private" / "credentials.json",
    )
    config = Settings(database_url=database, upload_dir=tmp_path / "files")
    await rebind_cli.provision(args, config)
    first = json.loads(args.credentials.read_text())
    grants = (await client.get(path, headers=admin)).json()[0]
    assert set(grants["group_ids"]) == {existing, first["group_id"]}
    await rebind_cli.provision(args, config)
    second = json.loads(args.credentials.read_text())
    assert first["user_id"] == second["user_id"] and first["group_id"] == second["group_id"]
    assert first["authorization_revision"] == second["authorization_revision"]
    with psycopg.connect(database) as conn:
        assert (
            conn.execute("SELECT count(*) FROM tokens WHERE user_id=%s", (second["user_id"],)).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT count(*) FROM model_attempts").fetchone()[0] == 0
    second.pop("group_id")
    args.credentials.write_text(json.dumps(second), encoding="utf-8")
    with pytest.raises(ValueError, match="同名用户组"):
        await rebind_cli.provision(args, config)
    assert (await client.get(path, headers=admin)).json()[0]["group_ids"] == grants["group_ids"]


def test_frozen_execution_checks_copied_approval_and_packet(rebinding, tmp_path):
    out = tmp_path / "packet"
    packet = prepare_rebinding(rebinding[0], out, *rebinding[1:])
    approval = approved(tmp_path, packet)
    frozen = tmp_path / "experiment"
    frozen.mkdir()
    (frozen / "rebind-approval.json").write_bytes(approval.read_bytes())
    (frozen / "rebind-review-packet.json").write_bytes((out / "review-packet.json").read_bytes())
    digest = hashlib.sha256(approval.read_bytes()).hexdigest()
    verify_frozen_rebinding(frozen, digest)
    value = json.loads((frozen / "rebind-review-packet.json").read_text())
    value["cases"] += 1
    (frozen / "rebind-review-packet.json").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="审核包发生变化"):
        verify_frozen_rebinding(frozen, digest)
