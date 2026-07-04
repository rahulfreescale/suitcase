"""Semantic response cache backed by a vector database (OpenSearch k-NN).

Running the full agent costs 2-50s and several LLM calls. Many questions are
near-duplicates ("best food in Lisbon?" vs "where to eat in Lisbon"). A
semantic cache skips the whole pipeline when a similar question was answered
recently.

Why a vector DB (not a Python scan):
  Matching means "find the nearest stored query embedding to this new one." A
  naive version loops over every cached entry computing cosine in Python — fine
  for hundreds, hopeless for millions. Production systems put the embeddings in
  a vector index that does approximate-nearest-neighbour (ANN) search in
  optimised native code. We already run OpenSearch for document retrieval, so we
  reuse it: the cache is just a SECOND index (`semantic_cache`) with a k-NN
  vector field. OpenSearch does the similarity search; we just read the top hit.

Design choices (unchanged from the concept):
  - We embed and match on the QUERY, not the answer.
  - We cache on the RESOLVED query ("that city" -> "Lisbon"), so hits
    are context-independent and reusable across sessions.
  - The similarity threshold is the tuning knob (too loose = wrong answers, too
    tight = few hits). TTL expires entries so a re-ingest doesn't serve staleness.

Local OpenSearch -> Amazon OpenSearch Service is the same code (auth differs via
config), exactly like the document index.
"""
from __future__ import annotations
import time
import hashlib
from app.config import get_settings

_s = get_settings()


# ---- index management --------------------------------------------------------
def _cache_index_body() -> dict:
    # Same k-NN shape as the document index: HNSW + lucene + cosine similarity.
    return {
        "settings": {"index": {"knn": True}},
        "mappings": {"properties": {
            "embedding": {"type": "knn_vector", "dimension": _s.embed_dim,
                          "method": {"name": "hnsw", "engine": "lucene",
                                     "space_type": "cosinesimil"}},
            "query": {"type": "text"},
            "answer": {"type": "text"},
            "result_json": {"type": "text", "index": False},  # stored, not searched
            "cached_at": {"type": "long"},
        }},
    }


def create_cache_index() -> None:
    """Create the semantic_cache index if missing (run once, like make index)."""
    from app.stores.vector_opensearch import get_client
    c = get_client()
    if c.indices.exists(_s.cache_index):
        print(f"cache index '{_s.cache_index}' already exists")
        return
    c.indices.create(_s.cache_index, body=_cache_index_body())
    print(f"created cache index '{_s.cache_index}'")


# ---- lookup / store ----------------------------------------------------------
def lookup(query: str) -> dict | None:
    """Return a cached result for a semantically-similar query, or None.

    OpenSearch k-NN finds the nearest stored query embedding; we accept it only
    if its cosine similarity clears the threshold and it hasn't expired.
    """
    if not _s.cache_enabled:
        return None
    import json
    from app.embeddings import embed_one
    from app.stores.vector_opensearch import get_client
    try:
        c = get_client()
        if not c.indices.exists(_s.cache_index):
            return None
        q_emb = embed_one(query)
        res = c.search(index=_s.cache_index, body={
            "size": 1,
            "query": {"knn": {"embedding": {"vector": q_emb, "k": 1}}},
        })
        hits = res.get("hits", {}).get("hits", [])
        if not hits:
            return None
        top = hits[0]
        # OpenSearch cosinesimil score = 1/(1+distance)-ish; recompute true cosine
        # for a clean, interpretable threshold instead of trusting the raw _score.
        src = top["_source"]
        sim = _cosine(q_emb, src["embedding"])
        if sim < _s.cache_similarity_threshold:
            return None
        # TTL check at read time (belt and braces alongside any ILM policy).
        if _s.cache_ttl_s and (time.time() - src.get("cached_at", 0)) > _s.cache_ttl_s:
            return None
        payload = json.loads(src.get("result_json", "{}"))
        payload["_cache"] = {"hit": True, "similarity": round(sim, 3),
                             "cached_query": src.get("query", "")}
        return payload
    except Exception as e:
        print(f"[cache] lookup skipped: {type(e).__name__}: {e}")
        return None


def store(query: str, result: dict) -> None:
    """Cache a query's result: embed the query and index it in OpenSearch."""
    if not _s.cache_enabled:
        return
    import json
    from app.embeddings import embed_one
    from app.stores.vector_opensearch import get_client
    try:
        c = get_client()
        if not c.indices.exists(_s.cache_index):
            create_cache_index()
        q_emb = embed_one(query)
        doc_id = hashlib.sha1(query.encode()).hexdigest()[:16]  # dedup by query
        c.index(index=_s.cache_index, id=doc_id, body={
            "embedding": q_emb,
            "query": query,
            "answer": result.get("answer", ""),
            "result_json": json.dumps(result),
            "cached_at": int(time.time()),
        })
        c.indices.refresh(_s.cache_index)
    except Exception as e:
        print(f"[cache] store skipped: {type(e).__name__}: {e}")


def stats() -> dict:
    """Entry count for inspection."""
    if not _s.cache_enabled:
        return {"enabled": False}
    try:
        from app.stores.vector_opensearch import get_client
        c = get_client()
        if not c.indices.exists(_s.cache_index):
            return {"enabled": True, "entries": 0, "index": _s.cache_index}
        c.indices.refresh(_s.cache_index)
        n = c.count(index=_s.cache_index)["count"]
        return {"enabled": True, "entries": n, "index": _s.cache_index}
    except Exception as e:
        return {"enabled": True, "error": str(e)}


def _cosine(a: list[float], b: list[float]) -> float:
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
