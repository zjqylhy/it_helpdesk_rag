"""费用仅由已知用量和冻结价格推算；不把报价称为账单。"""

import math

from helpdesk.application.evaluation import ExperimentLedger
from helpdesk.domain.models import Row


def validate_prices(value: Row) -> Row:
    if not isinstance(value.get("version"), str) or not value["version"] or value.get("currency") != "CNY":
        raise ValueError("价格必须声明版本及 CNY 币种")
    for kind in ("chat", "embedding", "rerank"):
        fields = ("input_per_million", "output_per_million") if kind == "chat" else ("input_per_million",)
        for field in fields:
            rate = value[kind][field]
            if type(rate) not in (int, float) or not math.isfinite(rate) or rate < 0:
                raise ValueError("价格必须是非负有限数值")
    return value


def fee_summary(ledger: ExperimentLedger, keys: list[str], prices: Row | None) -> Row:
    if prices is None:
        return {"price_version": None, "fee": None, "known_fee_estimate": None}
    total, unpriced = 0.0, 0
    for key in keys:
        for stage, incoming, outgoing in ledger.conn.execute(
            "SELECT stage,input_tokens,output_tokens FROM attempts WHERE run_id=?", (key,)
        ):
            kind = stage if stage in {"embedding", "rerank"} else "chat"
            if incoming is None or (kind == "chat" and outgoing is None):
                unpriced += 1
                continue
            rates = prices[kind]
            total += (
                incoming * rates["input_per_million"]
                + (outgoing * rates["output_per_million"] if kind == "chat" else 0)
            ) / 1000000
    return {
        "price_version": prices["version"],
        "currency": "CNY",
        "fee": round(total, 8) if not unpriced else None,
        "known_fee_estimate": round(total, 8),
        "unpriced_attempts": unpriced,
        "actual_bill": None,
    }


def paired_retrieval(results: list[Row]) -> list[Row]:
    pairs = []
    for case_id in sorted({r["case_id"] for r in results}):
        for variant in sorted({r["variant"] for r in results}):
            arms = {r["strategy"]: r for r in results if r["case_id"] == case_id and r["variant"] == variant}
            if set(arms) != {"baseline", "split-v1"}:
                continue
            metrics = {}
            for field in (
                "candidates_coverage",
                "fused_coverage",
                "reranked_coverage",
                "context_coverage",
                "recall_at_5",
            ):
                left, right = (arms[s].get(field, {}).get("recall") for s in ("baseline", "split-v1"))
                metrics[field] = {
                    "baseline": left,
                    "split": right,
                    "delta": right - left if left is not None and right is not None else None,
                }
            pairs.append({"case_id": case_id, "variant": variant, "metrics": metrics})
    return pairs
