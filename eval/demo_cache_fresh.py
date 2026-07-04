"""Clean paraphrase test: clears the cache, stores ONE base question, then tries
two paraphrases (should HIT via the 0.87 threshold at sub-1.0 similarity) and one
genuinely different question (should MISS). This actually exercises the semantic
threshold, unlike re-running the same questions (which just self-match at 1.0).
"""
import time
import uuid
from app.agents.graph import graph_with_checkpointer, run_with_memory
from app.stores.cache import stats

QS = [
    ("How many studies were done on rats?",                    "miss then store"),
    ("What is the number of studies conducted on rats?",       "paraphrase -> HIT"),
    ("How many rat studies are there?",                        "paraphrase -> HIT"),
    ("What were the cardiovascular effects of BAY-7 in dogs?", "different -> MISS"),
]


def main():
    print(f"cache before: {stats()}")
    with graph_with_checkpointer() as g:
        for q, expect in QS:
            t = time.time()
            sess = "sess-fresh-" + uuid.uuid4().hex[:4]   # fresh session each time
            f = run_with_memory(g, q, str(uuid.uuid4()), session_id=sess, user_id="u")
            c = f.get("_cache")
            tag = f"HIT  sim={c['similarity']}" if c else "MISS"
            print(f"{time.time()-t:5.1f}s  {tag:16}| expect: {expect:20} | {q}")
    print(f"cache after: {stats()}")


if __name__ == "__main__":
    main()
