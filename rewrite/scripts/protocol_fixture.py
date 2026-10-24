"""新旧进程共同使用的合成外部输入与 HTTP 替身，不含检索/编排实现。"""

import json

import httpx


def handler(fixture, calls):
    def respond(request):
        body = json.loads(request.content)
        calls.append({"path": request.url.path, "body": body})
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": i, "embedding": [1.0, 0.2, 0.3]}
                        for i, _ in enumerate(body["input"])
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 0},
                },
            )
        if request.url.path.endswith("/rerank"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"index": i, "relevance_score": 1 - i / 1000}
                        for i in range(min(body["top_n"], len(body["documents"])))
                    ],
                    "usage": {"input_tokens": 5, "output_tokens": 0},
                },
            )
        if body.get("stream"):
            answer = fixture.get("answer", "合成步骤 [C1]。")
            data = {
                "choices": [{"delta": {"content": answer}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5},
            }
            return httpx.Response(
                200,
                text="data: "
                + json.dumps(data, ensure_ascii=False)
                + "\n\ndata: [DONE]\n\n",
                headers={"Content-Type": "text/event-stream"},
            )
        payload = json.loads(body["messages"][1]["content"])
        if "available_knowledge_bases" in payload:
            value = {
                "query": payload["question"],
                "behavior": fixture.get("scope_behavior", "search"),
                "kb_ids": fixture.get(
                    "route_ids", [k["id"] for k in payload["available_knowledge_bases"]]
                ),
                "requires_all": fixture.get("requires_all", False),
                "message": "请补充推送应用、平台或通道。",
                "option_ids": [],
            }
        elif "history" in payload:
            value = {"query": fixture.get("rewritten", payload["question"])}
        elif "subqueries" in body["messages"][0]["content"]:
            value = {"subqueries": fixture.get("split_parts", [])}
        else:
            value = {
                "behavior": fixture.get("sufficiency", "answer"),
                "reason": "合成证据判断",
                "evidence_labels": ["C1"],
            }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(value, ensure_ascii=False)},
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    return respond


def semantic_calls(calls):
    result = []
    for call in calls:
        body = call["body"]
        if "messages" in body:
            result.append(
                {
                    "kind": "generate" if body.get("stream") else "structured",
                    "messages": body["messages"],
                    "max_tokens": body["max_tokens"],
                    "temperature": body["temperature"],
                    "response_format": body.get("response_format"),
                }
            )
        elif "input" in body:
            result.append(
                {
                    "kind": "embedding",
                    "texts": body["input"],
                    "dimensions": body["dimensions"],
                    "encoding_format": body.get("encoding_format"),
                }
            )
        else:
            result.append(
                {
                    "kind": "rerank",
                    "query": body["query"],
                    "documents": body["documents"],
                    "top_n": body["top_n"],
                }
            )
    return result
