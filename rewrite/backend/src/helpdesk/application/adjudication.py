import json
from pathlib import Path

from helpdesk.application.evaluation import fingerprint, write_new
from helpdesk.domain.models import Row


def adjudicate(directory: Path, decisions_file: Path, out: Path) -> None:
    packet = json.loads((directory / "blind-review.json").read_text(encoding="utf-8"))
    mapping = json.loads((directory / "private-mapping.json").read_text(encoding="utf-8"))
    results = json.loads((directory / "results.json").read_text(encoding="utf-8"))
    expected = {p["review_id"]: p for p in packet}
    decisions = [
        json.loads(line) for line in decisions_file.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    by_id: dict[str, Row] = {}
    for decision in decisions:
        key = decision.get("review_id")
        if (
            key not in expected
            or key in by_id
            or type(decision.get("correct")) is not bool
            or type(decision.get("unsupported")) is not bool
            or not isinstance(decision.get("omitted_requirements"), list)
            or not str(decision.get("reviewer", "")).strip()
            or not str(decision.get("reason", "")).strip()
        ):
            raise ValueError("人工裁决缺字段、重复或引用未知记录")
        if decision["correct"] and expected[key]["status"] in {"error", "interrupted"}:
            raise ValueError("失败或中断不能标为正确")
        if decision["correct"] and (decision["unsupported"] or decision["omitted_requirements"]):
            raise ValueError("存在无依据或遗漏时不能标为正确")
        by_id[key] = decision
    reviewed = {mapping[key]: decision for key, decision in by_id.items()}
    cases = {mapping[p["review_id"]]: p["case"] for p in packet}
    groups = []
    for strategy, variant in sorted({(r["strategy"], r["variant"]) for r in results}):
        rows = [r for r in results if r["strategy"] == strategy and r["variant"] == variant]
        complete = all(r["key"] in reviewed for r in rows)
        answerable = [r for r in rows if cases[r["key"]]["expected_behavior"] == "answer"]
        groups.append(
            {
                "strategy": strategy,
                "variant": variant,
                "recorded": len(rows),
                "reviewed": sum(r["key"] in reviewed for r in rows),
                "human_correctness": sum(reviewed[r["key"]]["correct"] for r in rows) / len(rows)
                if complete and rows
                else None,
                "false_refusal": sum(r["status"] == "refused" for r in answerable) / len(answerable)
                if complete and answerable
                else None,
                "unsupported_answer_rate": sum(reviewed[r["key"]]["unsupported"] for r in rows) / len(rows)
                if complete and rows
                else None,
                "omission_rate": sum(bool(reviewed[r["key"]]["omitted_requirements"]) for r in rows)
                / len(rows)
                if complete and rows
                else None,
                "correct_to_wrong": None,
            }
        )
    # 成对净变化只计算两组均经人工裁决的题，原失败及未裁决数另列。
    pairs = []
    for case_id in sorted({r["case_id"] for r in results}):
        for variant in sorted({r["variant"] for r in results}):
            arms = {r["strategy"]: r for r in results if r["case_id"] == case_id and r["variant"] == variant}
            if set(arms) != {"baseline", "split-v1"} or any(r["key"] not in reviewed for r in arms.values()):
                continue
            pairs.append(
                {
                    "case_id": case_id,
                    "variant": variant,
                    "baseline_correct": reviewed[arms["baseline"]["key"]]["correct"],
                    "split_correct": reviewed[arms["split-v1"]["key"]]["correct"],
                    "extra_calls": arms["split-v1"].get("calls", 0) - arms["baseline"].get("calls", 0)
                    if arms["split-v1"].get("calls") is not None and arms["baseline"].get("calls") is not None
                    else None,
                }
            )
    out.mkdir(parents=True, exist_ok=False)
    write_new(out / "decisions.json", decisions)
    write_new(
        out / "summary.json",
        {
            "review_packet_hash": fingerprint(packet),
            "source_results_hash": fingerprint(results),
            "groups": groups,
            "pairs": pairs,
            "human_decisions": len(decisions),
            "pending_recorded": len(results) - len(decisions),
            "model_calls": 0,
            "note": "本阶段未运行回答修正，误改率不适用；未执行题目见原 summary 的 pending。",
        },
    )
