"""仅测试：在独立解释器加载冻结旧源码，捕获实际模型 HTTP；新应用不能调用此入口。"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from protocol_fixture import handler, semantic_calls


async def capture(fixture):
    from helpdesk.config import Settings
    from helpdesk.pipeline import Pipeline
    from helpdesk.providers import Models
    from helpdesk.store import combine_snapshots
    import httpx

    class FixtureStore:
        s = SimpleNamespace(context_budget=8000)

        async def scope_snapshot(self, ids):
            return combine_snapshots(
                [m for m in fixture["members"] if not ids or m["kb_id"] in ids]
            )

        async def snapshot(self, kb_id, snapshot_id=None):
            return next(m for m in fixture["members"] if m["kb_id"] == kb_id)

        async def corpus(self, snapshot):
            return [
                c
                for c in fixture["chunks"]
                if c["version_id"] in snapshot["version_ids"]
            ]

        async def vector_search(self, snapshot, vector):
            allowed = {
                c["id"]
                for c in fixture["chunks"]
                if c["version_id"] in snapshot["version_ids"]
            }
            return [{"id": key} for key in fixture["vector_order"] if key in allowed][
                :20
            ]

    settings = Settings(
        _env_file=None,
        chat_gateway_enabled=True,
        retrieval_gateway_enabled=True,
        model_gateway_base_url="https://localhost/v1",
        gateway_api_key="synthetic-key",
        model_gateway_ca_file="",
        model_gateway_connect_host="",
        llm_model="fixture-chat",
        embedding_model="fixture-embed",
        rerank_model="fixture-rank",
        embedding_dimension=3,
    )
    calls = []
    models = Models(settings, transport=httpx.MockTransport(handler(fixture, calls)))
    pipeline = Pipeline(FixtureStore(), models, experiment=fixture["strategy"])
    try:
        events = [
            item
            async for item in pipeline.events(
                None,
                fixture["question"],
                fixture.get("history", []),
                kb_ids=fixture.get("kb_ids", []),
            )
        ]
        terminal = next(data["status"] for event, data in events if event == "done")
        return {
            "id": fixture["id"],
            "calls": semantic_calls(calls),
            "selected_ids": [c["id"] for c in pipeline.last_evidence],
            "fused_ids": pipeline.last_trace.get("ranking", {}).get("fused", []),
            "status": terminal,
            "answer": "".join(
                data["text"] for event, data in events if event == "answer_delta"
            ),
            "source": str(sys.modules["helpdesk.pipeline"].__file__),
        }
    finally:
        await models.close()


def main():
    # 旧包路径由父进程提供，不能通过同名 helpdesk 混入新解释器。
    source = Path(sys.argv[1]).resolve(strict=True)
    if not source.is_relative_to(ROOT / "data"):
        raise ValueError("只能使用 data 下的冻结测试副本")
    sys.path.insert(0, str(source / "rag"))
    fixtures = json.loads(sys.stdin.buffer.read().decode("utf-8"))

    async def run():
        return [await capture(fixture) for fixture in fixtures]

    with asyncio.Runner(
        loop_factory=asyncio.SelectorEventLoop
        if sys.platform == "win32"
        else asyncio.new_event_loop
    ) as runner:
        result = runner.run(run())
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
