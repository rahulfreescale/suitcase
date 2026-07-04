"""Demo: semantic caching.

Ask a question (cache miss — runs the full pipeline, slow). Then ask a
PARAPHRASE of it (cache hit — returns instantly, skipping the pipeline). Then a
genuinely different question (miss again). Proves the cache matches on meaning,
not exact string.

  make up
  LLM_MODEL_CHAIN="anthropic/claude-haiku-4-5,gpt-4o-mini" python -m eval.demo_cache
"""
from __future__ import annotations
import os
import time
import uuid

os.environ.setdefault("LANGFUSE_TRACING_ENVIRONMENT", "cache-demo")

from app.agents.graph import graph_with_checkpointer, run_with_memory
from app.stores.cache import stats
from app.observability import request_trace, flush as lf_flush

PAIRS = [
    ("How many studies were done on rats?",          "miss (first time)"),
    ("What is the number of studies conducted on rats?", "HIT expected (paraphrase)"),
    ("How many rat studies are there?",              "HIT expected (paraphrase)"),
    ("Summarize the cardiovascular effects of BAY-7 in dogs.", "miss (different topic)"),
]


def main():
    print(f"cache before: {stats()}\n" + "=" * 72)
    with graph_with_checkpointer() as graph:
        for q, expect in PAIRS:
            tid = str(uuid.uuid4())
            t0 = time.time()
            with request_trace("ask", q, session_id="sess-cache", user_id="user_cache"):
                final = run_with_memory(graph, q, tid,
                                        session_id="sess-cache", user_id="user_cache")
            dt = time.time() - t0
            c = final.get("_cache")
            tag = (f"CACHE HIT (sim={c['similarity']}, matched: '{c['cached_query'][:40]}')"
                   if c else "cache miss -> ran pipeline")
            print(f"\nQ: {q}\n   expected: {expect}\n   {dt:5.2f}s  {tag}")
    lf_flush()
    print("\n" + "=" * 72)
    print(f"cache after: {stats()}")
    print("Cache hits should return in a fraction of a second vs multi-second misses,\n"
          "and should fire on PARAPHRASES (semantic match), not just identical strings.")


if __name__ == "__main__":
    main()
