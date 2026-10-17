import json
from importlib.resources import files

from helpdesk.application.evaluation import EvalCase, plan
from helpdesk.application.model_session import Budget
from helpdesk.domain.models import Row


def proposal(cases: list[EvalCase], bundle: Row) -> Row:
    prompts = json.loads(files("helpdesk.assets").joinpath("prompts.json").read_text(encoding="utf-8"))
    dev = [c for c in cases if c.split == "dev"]
    rows: list[Row] = []
    for case in dev:
        members = (
            [m for m in bundle["members"] if m["kb_id"] in case.kb_ids]
            if case.scope_mode == "selected"
            else [m for m in bundle["members"] if m["default_search"]]
        )
        chunks = [c for c in bundle["corpus"]["chunks"] if c["kb_id"] in {m["kb_id"] for m in members}]
        query = (
            case.question
            if len(members) != 1
            else f"适用组织：{members[0]['organization']}\n问题：{case.question}"
        )
        upper_texts = sorted(
            [
                f"知识库：{c['knowledge_base_name']}；适用组织：{c['organization']}；文档：{c['title']}\n{c['text']}"
                for c in chunks
            ],
            key=lambda s: len(s.encode()),
            reverse=True,
        )[: min(40, 20 * len(members))]
        largest = sorted(chunks, key=lambda c: len(c["text"].encode()), reverse=True)[:5]
        evidence = [
            {
                "label": f"C{i + 1}",
                "text": c["text"],
                "title": c["title"],
                "section": c["locator"].get("section", ""),
                "knowledge_base": c["knowledge_base_name"],
                "organization": c["organization"],
            }
            for i, c in enumerate(largest)
        ]

        def structured(system: str, payload: Row) -> int:
            return Budget.estimate({"system": system, "payload": payload}, 2048)

        scope_units = 0
        if len(members) > 1:
            scope_units = structured(
                prompts["scope"],
                {
                    "question": case.question,
                    "history": case.history,
                    "available_knowledge_bases": [
                        {"id": m["kb_id"], "name": m["name"], "organization": m["organization"]}
                        for m in members
                    ],
                },
            )
        elif case.history:
            scope_units = structured(prompts["rewrite"], {"question": case.question, "history": case.history})
        base = (
            scope_units
            + Budget.estimate([query], 0)
            + Budget.estimate({"query": query, "documents": upper_texts}, 0)
        )
        base += structured(
            prompts["sufficiency"],
            {
                "question": query,
                "evidence": evidence,
                "required_organizations": [m["organization"] for m in members],
            },
        )
        base += Budget.estimate(
            {
                "system": prompts["answer"] + prompts["comparison_suffix"],
                "payload": {"question": query, "evidence": evidence},
            },
            2048,
        )
        extra = structured(prompts["split"], {"question": query}) + 2 * Budget.estimate(
            [f"原问题及适用条件：{query}\n重点子任务：{query}"], 0
        )
        base_calls = 4 + int(len(members) > 1 or bool(case.history))
        rows.append(
            {
                "case_id": case.id,
                "baseline_no_retry_call_upper": base_calls,
                "split_no_retry_call_upper": base_calls + 3,
                "baseline_reservation_envelope": base,
                "split_reservation_envelope": base + extra,
                "exceeds_default_48000": base > 48000 or base + extra > 48000,
            }
        )
    return {
        "schema": "rewrite-experiment-proposal-v1",
        "status": "pending_execution_approval",
        "engine": "rewrite",
        "split": "dev",
        "seed": 12,
        "planned_outputs": 2 * len(dev),
        "order": plan(dev, ["baseline", "split-v1"], ["D"], 12),
        "http_limit_proposed": 150,
        "generation_no_retry_call_upper": sum(
            r["baseline_no_retry_call_upper"] + r["split_no_retry_call_upper"] for r in rows
        ),
        "includes_retries": True,
        "judge_calls_proposed": 0,
        "actual_model_calls": 0,
        "test_cases_authorized": False,
        "inherits_legacy_budget_approval": False,
        "current_price_version": None,
        "fee_estimate": None,
        "reservation_estimator": "utf8-bytes-plus-max-output-v1",
        "reservation_rows": rows,
        "reservation_exceeds_default_cases": sum(r["exceeds_default_48000"] for r in rows),
        "reservation_assumptions": [
            "eligible-chunk upper envelope, not observed vector/rerank output",
            "question preserved; future model rewrite length is unknown",
            "five largest chunks overestimate context; required organizations use whole scope",
            "estimated reservation units are not provider tokens; no guaranteed completion or cash ceiling",
        ],
        "not_included": [
            "legacy engine paid run",
            "formal test split",
            "automatic judging",
            "supplementary retrieval",
            "answer checking",
        ],
    }
