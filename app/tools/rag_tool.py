"""RAG tool: the full query-time pipeline, returning grounded evidence + citations.

Stages (matching the tutorial):
  1 keywords  2 metadata filter  3 expansion  4 hybrid search
  5 rerank    6 assemble context + citations
"""
from app.retrieval.keywords import extract_keywords
from app.retrieval.filters import generate_filter
from app.retrieval.expansion import expand
from app.retrieval.hybrid import hybrid_search
from app.retrieval.rerank import rerank
from app.config import get_settings

_s = get_settings()


def run_rag(query: str) -> dict:
    keywords = extract_keywords(query)
    meta_filter = generate_filter(query)
    expanded = expand(query)
    candidates = hybrid_search(expanded, meta_filter, keywords)
    top = rerank(query, candidates)

    # Grounding gate: is the BEST chunk actually relevant enough to answer?
    top_score = top[0].get("rerank_score", 0.0) if top else 0.0
    grounded = bool(top) and top_score >= _s.min_relevance_score

    citations, context_blocks = [], []
    for i, c in enumerate(top, 1):
        cite = {
            "n": i, "city": c.get("city"), "section": c.get("section"),
            "page": c.get("page"), "quote": (c.get("text", "")[:240]),
        }
        citations.append(cite)
        context_blocks.append(
            f"[{i}] ({cite['city']} guide, p.{cite['page']}) {c.get('text','')}"
        )

    return {
        "tool": "rag",
        "trace": {"keywords": keywords, "filter": meta_filter,
                  "expanded": expanded, "candidates": len(candidates),
                  "kept": len(top), "top_score": f"{top_score:.3f}"},
        "context": "\n\n".join(context_blocks),
        "citations": citations,
        "found": grounded,
    }
