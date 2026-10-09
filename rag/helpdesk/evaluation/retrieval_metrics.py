from .dataset import anchor_interval, same_locator

def covered(anchor, chunks, corpus):
    start, end = anchor_interval(anchor, corpus)
    intervals = sorted((c['locator']['start'], c['locator']['end']) for c in chunks
                       if c['version_id'] == anchor.source_version and same_locator(c['locator'], anchor.locator))
    cursor = start
    for left, right in intervals:
        if left > cursor:
            break
        if right > cursor:
            cursor = right
        if cursor >= end:
            return True
    return False

def retrieval_metrics(case, chunks, corpus):
    if case.expected_behavior != 'answer':
        return {'recall_at_5': None, 'full_coverage': None, 'mrr_at_5': None}
    chunks = chunks[:5]
    groups = case.evidence_groups
    hits = [any(covered(a, chunks, corpus) for a in group) for group in groups]
    first = next((rank for rank in range(1,len(chunks)+1)
                  if any(covered(a, chunks[:rank], corpus) for group in groups for a in group)), None)
    return {'recall_at_5': sum(hits)/len(groups), 'full_coverage': int(all(hits)), 'mrr_at_5': 1/first if first else 0}
