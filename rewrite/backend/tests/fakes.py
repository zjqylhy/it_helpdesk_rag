import asyncio

from helpdesk.domain.rag import Reply, TextDelta, Usage


class FakeModels:
    def __init__(self):
        self.calls = []
        self.behavior = "answer"
        self.answer = "请打开设置 [C1]。"
        self.requires_all = False
        self.wait_generate = None
        self.entered = asyncio.Event()

    async def structured(self, stage, system, payload, limit):
        self.calls.append((stage, payload))
        if stage == "scope":
            value = {
                "query": payload["question"],
                "behavior": "clarify" if self.behavior == "clarify" else "search",
                "kb_ids": [k["id"] for k in payload["available_knowledge_bases"]],
                "requires_all": self.requires_all,
                "message": "",
                "option_ids": [],
            }
        elif stage == "rewrite":
            value = {"query": payload["question"]}
        elif stage == "split":
            value = {"subqueries": ["第一项", "第二项"]}
        else:
            value = {"behavior": self.behavior, "reason": "fixture", "evidence_labels": ["C1"]}
        return Reply(value, Usage(5, 2))

    async def embed(self, texts):
        self.calls.append(("embedding", texts))
        return [[1.0, 0.2, 0.3] for _ in texts], Usage(5, 0)

    async def rank(self, query, texts):
        self.calls.append(("rerank", texts))
        return list(range(len(texts))), Usage(5, 0)

    async def generate(self, system, payload, limit):
        self.calls.append(("generate", payload))
        self.entered.set()
        if self.wait_generate:
            await self.wait_generate.wait()
        yield TextDelta(self.answer[:3])
        yield TextDelta(self.answer[3:])
        yield TextDelta(finish_reason="stop", usage=Usage(5, 5))
