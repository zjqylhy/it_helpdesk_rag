import re
from collections import defaultdict

from helpdesk.domain.rag import Chunk


def bm25_order(query: str, texts: list[str], ids: list[str] | None = None) -> list[int]:
    import jieba  # type: ignore[import-untyped]
    from rank_bm25 import BM25Okapi  # type: ignore[import-untyped]

    def tokens(text: str) -> list[str]:
        return [t.lower() for t in jieba.lcut(text) if t.strip()]

    if not texts:
        return []
    corpus = [tokens(t) or [""] for t in texts]
    scores = BM25Okapi(corpus).get_scores(tokens(query))
    return sorted(range(len(texts)), key=lambda i: (-scores[i], ids[i] if ids is not None else str(i)))[:20]


def rrf(channels: list[list[Chunk]], limit: int = 20) -> list[Chunk]:
    scores: dict[str, float] = defaultdict(float)
    chunks = {}
    for channel in channels:
        for rank, chunk in enumerate(channel, 1):
            chunks[chunk.id] = chunk
            scores[chunk.id] += 1 / (60 + rank)
    return [chunks[key] for key in sorted(scores, key=lambda k: (-scores[k], k))[:limit]]


def round_robin(groups: list[list[Chunk]], limit: int = 40) -> list[Chunk]:
    result = []
    seen = set()
    for rank in range(max(map(len, groups), default=0)):
        for group in groups:
            if rank < len(group) and group[rank].id not in seen:
                result.append(group[rank])
                seen.add(group[rank].id)
                if len(result) == limit:
                    return result
    return result


def select_context(ranked: list[Chunk], required: list[str]) -> list[Chunk]:
    priority = []
    for kb_id in required:
        found = next((c for c in ranked if c.kb_id == kb_id), None)
        if found and found not in priority:
            priority.append(found)
    ordered = priority + [c for c in ranked if c not in priority]
    selected: list[Chunk] = []
    characters = 0
    for chunk in ordered:
        if len(selected) == 5:
            break
        if characters + len(chunk.text) > 8000:
            continue
        selected.append(chunk)
        characters += len(chunk.text)
    return selected


def citation_indices(answer: str, count: int, required: list[str], selected: list[Chunk]) -> list[int]:
    from helpdesk.domain.errors import AppError

    references = re.findall(r"\[C([^\]]*)\]", answer)
    if (
        not references
        or len(re.findall(r"\[C", answer)) != len(references)
        or any(not re.fullmatch(r"[1-9]\d*", ref) for ref in references)
    ):
        raise AppError("invalid_citations", 502, "回答缺少有效引用")
    indices = list(dict.fromkeys(int(i) - 1 for i in references))
    if any(i < 0 or i >= count for i in indices):
        raise AppError("invalid_citations", 502, "回答包含无效引用")
    if not set(required).issubset({selected[i].kb_id for i in indices}):
        raise AppError("incomplete_comparison", 502, "比较回答缺少必要对象引用")
    return indices
