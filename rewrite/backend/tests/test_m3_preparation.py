import hashlib
import importlib
import json
from pathlib import Path

import httpx
import pytest
from test_models_and_limits import settings as provider_settings

from helpdesk.adapters.models.client import model_client
from helpdesk.adapters.models.gateway import HttpModels
from helpdesk.application.evaluation import Anchor, covered, load_cases
from helpdesk.application.releases import canonical_hash, prepare_release
from helpdesk.domain.errors import AppError
from helpdesk.domain.rag import BoundScope
from helpdesk.entrypoints.evaluate import verify_source

ROOT = Path(__file__).resolve().parents[3]


async def test_preview_disables_all_inference_before_network():
    requests = []

    async def transport(request):
        requests.append(request)
        raise AssertionError("验收预览不得发送模型请求")

    settings = provider_settings().model_copy(update={"model_inference_enabled": False})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = HttpModels(settings, client)
        for call in (
            provider.structured("route", "system", {}, 10),
            provider.embed(["synthetic"]),
            provider.rank("synthetic", ["synthetic"]),
        ):
            with pytest.raises(AppError) as error:
                await call
            assert error.value.code == "inference_disabled"
        with pytest.raises(AppError):
            async for _ in provider.generate("system", {}, 10):
                pass
    assert not requests


def test_docker_gateway_retains_logical_host_and_sni():
    settings = provider_settings().model_copy(update={
        "chat_url": "https://localhost:9443/v1/chat/completions",
        "model_connect_host": "host.docker.internal",
    })
    provider = HttpModels(settings, httpx.AsyncClient())
    url, headers, extensions = provider.target("chat")
    assert str(url) == "https://host.docker.internal:9443/v1/chat/completions"
    assert headers["Host"] == "localhost:9443"
    assert extensions["sni_hostname"] == "localhost"


async def test_frozen_scope_wire_contract(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "rewrite/scripts"))
    harness = importlib.import_module("check_graph_protocol")
    frozen = json.loads(
        (Path(__file__).parent / "fixtures/scope-wire-expected.json").read_text(encoding="utf-8")
    )
    actual = await harness.current(harness.inputs())
    assert actual == frozen["results"]


def source_release(folder):
    folder.mkdir()
    text = "合成原文说明"
    chunk = {
        "id": "chunk",
        "version_id": "v",
        "kb_id": "kb",
        "title": "合成手册",
        "knowledge_base_name": "合成库",
        "organization": "合成学校",
        "text": text,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "locator": {"paragraph": 1, "page": 2, "section": "配置", "start": 0, "end": len(text)},
    }
    bundle = {
        "schema": "scope-eval-v1",
        "principal": {"user_id": "synthetic", "revision": 0},
        "members": [
            {
                "kb_id": "kb",
                "default_search": True,
                "version_ids": ["v"],
                "snapshot_id": "s",
                "name": "合成库",
                "organization": "合成学校",
            }
        ],
        "corpus": {
            "chunks": [chunk],
            "paragraphs": [
                {"version_id": "v", "locator": {"paragraph": 1, "page": 2, "section": "配置"}, "text": text}
            ],
        },
    }
    bundle["scope_version"] = canonical_hash(bundle)
    case = {
        "id": "case",
        "status": "active",
        "case_revision": 1,
        "split": "dev",
        "theme": "synthetic",
        "fact_topics": ["one-fact"],
        "old_topic_overlap": [],
        "scene": "control",
        "scope_version": bundle["scope_version"],
        "scope_mode": "default",
        "kb_ids": [],
        "expected_route_kb_ids": ["kb"],
        "expected_required_kb_ids": [],
        "question": "如何配置？",
        "history": [],
        "expected_behavior": "answer",
        "reference_answer": "合成答案",
        "required_facts": [text],
        "forbidden_facts": [],
        "evidence_groups": [
            [
                {
                    "source_version": "v",
                    "locator": {"paragraph": 1, "page": 2, "section": "配置"},
                    "quote": text,
                }
            ]
        ],
        "subtasks": [],
        "review_record": {
            "reviewer": "synthetic",
            "approved_at": "2026-10-17",
            "confirmation": "test only",
            "packet_hash": "synthetic-packet",
        },
    }
    manifest = {
        "schema": "scope-eval-v1",
        "version": "synthetic-v1",
        "dataset_hash": canonical_hash([case]),
        "scope_version": bundle["scope_version"],
    }
    approval = {
        "approved_case_ids": ["case"],
        "published_dataset_hash": manifest["dataset_hash"],
        "packet_hash": "synthetic-packet",
    }
    for name, value in [("corpus.json", bundle), ("manifest.json", manifest), ("approval.json", approval)]:
        (folder / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    (folder / "cases.jsonl").write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    return case, bundle


def test_source_conversion_preserves_default_scope_and_qualified_anchor(tmp_path):
    source = tmp_path / "source"
    case, bundle = source_release(source)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
    out = tmp_path / "new"
    report = prepare_release(source, out)
    converted = load_cases(out / "dev.jsonl")[0]
    assert converted.scope_mode == "default" and converted.kb_ids == []
    assert converted.evidence_groups[0][0].page == 2 and converted.evidence_groups[0][0].section == "配置"
    assert converted.source["review_record"] == case["review_record"]
    assert report["model_calls"] == 0 and report["cases"] == 1
    scope = BoundScope(
        ("kb",),
        ({"id": "kb", "name": "合成库", "organization": "合成学校"},),
        ("v",),
        ({"kb_id": "kb", "id": "s", "version_ids": ["v"]},),
        0,
        "synthetic",
    )
    chunks = [{**bundle["corpus"]["chunks"][0], "kb_name": "合成库"}]
    verify_source(converted, bundle, scope, chunks, "synthetic", 0)
    with pytest.raises(ValueError, match="主体"):
        verify_source(converted, bundle, scope, chunks, "other", 0)
    with pytest.raises(ValueError, match="原文"):
        verify_source(converted, bundle, scope, [{**chunks[0], "text": "篡改内容"}], "synthetic", 0)
    assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
    with pytest.raises(FileExistsError):
        prepare_release(source, out)


def test_tampered_release_rejected_before_creating_output(tmp_path):
    source = tmp_path / "source"
    source_release(source)
    case = json.loads((source / "cases.jsonl").read_text(encoding="utf-8"))
    case["question"] = "替换问题"
    (source / "cases.jsonl").write_text(json.dumps(case), encoding="utf-8")
    with pytest.raises(ValueError, match="校验"):
        prepare_release(source, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_page_and_section_are_part_of_anchor_identity():
    anchor = Anchor(version_id="v", paragraph=1, page=2, section="正确章节", start=0, end=2, quote="原文")
    rows = [
        {
            "version_id": "v",
            "text": "原文",
            "locator": {"paragraph": 1, "page": 1, "section": "正确章节", "start": 0, "end": 2},
        },
        {
            "version_id": "v",
            "text": "原文",
            "locator": {"paragraph": 1, "page": 2, "section": "错误章节", "start": 0, "end": 2},
        },
    ]
    assert not covered(anchor, rows)
    rows.append(
        {
            "version_id": "v",
            "text": "原文",
            "locator": {"paragraph": 1, "page": 2, "section": "正确章节", "start": 0, "end": 2},
        }
    )
    assert covered(anchor, rows)


async def test_embedding_alias_does_not_replace_stored_model_identity():
    settings = provider_settings().model_copy(
        update={"embedding_model": "actual-vector-model", "embedding_request_model": "embedding"}
    )
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 2, 3]}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await HttpModels(settings, client).embed(["synthetic"])
    assert seen[0]["model"] == "embedding" and seen[0]["encoding_format"] == "float"
    assert settings.embedding_model == "actual-vector-model"


def test_missing_private_ca_fails_without_insecure_fallback(tmp_path):
    settings = provider_settings().model_copy(update={"model_ca_file": tmp_path / "missing-ca.pem"})
    with pytest.raises(FileNotFoundError):
        model_client(settings)
