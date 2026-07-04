"""Stage 5: cross-encoder reranking down to the top-K chunks."""
from functools import lru_cache
from app.config import get_settings

_s = get_settings()


@lru_cache
def _model():
    from sentence_transformers import CrossEncoder
    return CrossEncoder(_s.rerank_model)


def rerank(query: str, chunks: list[dict], top_k: int | None = None):
    top_k = top_k or _s.rerank_top_k
    if not chunks:
        return []
    if _s.rerank_backend == "none":
        return chunks[:top_k]
    pairs = [(query, c.get("text", "")) for c in chunks]
    scores = _model().predict(pairs)
    for c, sc in zip(chunks, scores):
        c["rerank_score"] = float(sc)
    return sorted(chunks, key=lambda c: c["rerank_score"], reverse=True)[:top_k]
