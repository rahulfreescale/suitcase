"""Stage 4: weighted hybrid search across expanded queries.

For each expanded query: run semantic (kNN) + keyword search under the metadata
filter, min-max normalise each, blend 0.7 semantic / 0.3 keyword, then aggregate
across all expansions keeping each chunk's best score. Returns ~N candidates.
"""
from collections import defaultdict
from app.config import get_settings
from app.embeddings import embed
from app.retrieval.keywords import extract_keywords
from app.stores import vector_opensearch as vs

_s = get_settings()


def _norm(hits: list[dict]) -> dict[str, float]:
    if not hits:
        return {}
    scores = [h["score"] for h in hits]
    lo, hi = min(scores), max(scores)
    rng = (hi - lo) or 1.0
    return {h["id"]: (h["score"] - lo) / rng for h in hits}


def hybrid_search(expanded: list[str], meta_filter: dict, keywords: list[str]):
    by_id: dict[str, dict] = {}
    best: dict[str, float] = defaultdict(float)
    vectors = embed(expanded)                      # one batch embed call

    for q, vec in zip(expanded, vectors):
        sem = vs.semantic_search(vec, _s.hybrid_candidates, meta_filter)
        kw = vs.keyword_search(keywords or extract_keywords(q),
                               _s.hybrid_candidates, meta_filter)
        sem_n, kw_n = _norm(sem), _norm(kw)
        for h in sem + kw:
            by_id[h["id"]] = h
        for cid in set(sem_n) | set(kw_n):
            blended = (_s.semantic_weight * sem_n.get(cid, 0.0)
                       + _s.keyword_weight * kw_n.get(cid, 0.0))
            best[cid] = max(best[cid], blended)

    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    out = []
    for cid, score in ranked[: _s.hybrid_candidates]:
        item = dict(by_id[cid]); item["hybrid_score"] = round(score, 4)
        out.append(item)
    return out
