from types import SimpleNamespace

import pytest

from helpdesk.application.retrieval import citation_indices
from helpdesk.domain.errors import AppError
from helpdesk.graph.baseline import plan_query


@pytest.mark.parametrize(
    "parts,accepted",
    [
        (["第一项", "第二项"], True),
        (["第一项", "第一项"], False),
        (["原问题没有的组织", "第二项"], False),
        ([1, 2], False),
        (["第一项"], False),
        ([], False),
    ],
)
async def test_split_contiguous_parts_and_all_conditions(parts, accepted):
    calls = []

    class Models:
        async def structured(self, stage, system, payload):
            calls.append(stage)
            return {"subqueries": parts}

    question = "某学校 2026 年第一项和第二项如何处理？"
    runtime = SimpleNamespace(
        context=SimpleNamespace(run=SimpleNamespace(server_strategy_id="split-v1"), models=Models())
    )
    result = await plan_query({"query": question}, runtime)
    assert result["trace"]["split"]["accepted"] is accepted
    assert len(result["queries"]) == (3 if accepted else 1) and result["queries"][0] == question
    assert calls == ["split"]
    if accepted:
        assert all(question in q for q in result["queries"])
    runtime.context.run.server_strategy_id = "baseline"
    calls.clear()
    assert (await plan_query({"query": question}, runtime))["queries"] == [question]
    assert not calls


@pytest.mark.parametrize(
    "answer", ["正文 [C01][C1]", "正文 [C1,C2]", "正文 [Cfoo][C1]", "正文 [C1", "正文 [C0]", "没有来源"]
)
def test_exact_citation_labels_required(answer):
    with pytest.raises(AppError):
        citation_indices(answer, 2, [], [])
