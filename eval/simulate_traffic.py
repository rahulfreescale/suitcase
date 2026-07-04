"""Simulate production traffic: send a realistic, varied stream of questions.

This stands in for real users so you have live traffic to evaluate. Each request
flows through the full workflow and is recorded in the interaction log (the API
does this in `http` mode; this script does it in `inprocess` mode).

Usage:
  python -m eval.simulate_traffic --n 40 --concurrency 4            # vs running app
  python -m eval.simulate_traffic --n 20 --mode inprocess          # no server needed
  python -m eval.simulate_traffic --n 60 --paraphrase              # LLM-varied wording
"""
from __future__ import annotations
import argparse
import json
import random
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.config import get_settings

_s = get_settings()

# A realistic mix grounded in the sample data: document Qs, counting Qs,
# vague Qs (trigger clarify/reflection), and multi-step Qs.
SEED_QUERIES = [
    "Were piloerection and ataxia observed in study T123456-2?",
    "What clinical findings were seen at the high dose in study T123456-2?",
    "Were loose faeces recorded in study T123456-2?",
    "Were there any adverse findings in study T123457-9?",
    "Summarize the cardiovascular effects of BAY-7 in dogs.",
    "Did study T200110-4 show sustained cardiovascular effects?",
    "How many studies were done on rats?",
    "List all oral studies on compound BAY-1 with their doses.",
    "Which studies lasted longer than 30 days?",
    "What species were used across the studies?",
    "What was the dose in the 13-week BAY-1 study?",
    "Any safety concerns with BAY-1?",                       # vague
    "Tell me about the findings.",                            # vague
    "Compare the findings between T123456-2 and T123457-9.",  # multi-step
    "Across all rat studies, were any neurological signs reported?",  # multi-step
]


def paraphrase_pool(base: list[str], target_n: int) -> list[str]:
    """Optionally use the fast model to widen phrasing variety."""
    from app.llm import chat_json
    out = list(base)
    try:
        prompt = ("Rewrite each question below into one alternative phrasing a "
                  "different user might type. Return ONLY a JSON array of strings.\n\n"
                  + "\n".join(base))
        out += [str(x) for x in chat_json(
            [{"role": "user", "content": prompt}], model_chain=[_s.llm_fast_model] + _s.model_chain)]
    except Exception as e:
        print(f"[simulate] paraphrase skipped: {e}")
    random.shuffle(out)
    return (out * ((target_n // len(out)) + 1))[:target_n]


def _send_http(query: str, base_url: str) -> dict:
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(f"{base_url}/ask", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def run_http(queries: list[str], base_url: str, concurrency: int):
    ok = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {}
        for q in queries:
            futures[pool.submit(_send_http, q, base_url)] = q
            time.sleep(random.uniform(0.05, 0.3))  # jittered arrival
        for fut in as_completed(futures):
            q = futures[fut]
            try:
                res = fut.result()
                ok += 1
                print(f"  ✓ [{res.get('type','?')}] {q[:60]}")
            except Exception as e:
                print(f"  ✗ {q[:60]} -> {e}")
    return ok


def run_inprocess(queries: list[str]):
    from app.agents.graph import graph_with_checkpointer
    from app.stores.interactions import log_interaction
    from app.eval_utils import contexts_from_state
    ok = 0
    with graph_with_checkpointer() as graph:
        for q in queries:
            tid = str(uuid.uuid4())
            try:
                final = graph.invoke({"thread_id": tid, "query": q},
                                     {"configurable": {"thread_id": tid}, "recursion_limit": 40})
                if not final.get("needs_clarification"):
                    log_interaction(tid, q, final.get("answer", ""),
                                    contexts_from_state(final))
                ok += 1
                print(f"  ✓ {q[:60]}")
            except Exception as e:
                print(f"  ✗ {q[:60]} -> {e}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--mode", choices=["http", "inprocess"], default="http")
    ap.add_argument("--base-url", default="http://localhost:8080")
    ap.add_argument("--paraphrase", action="store_true")
    args = ap.parse_args()

    queries = (paraphrase_pool(SEED_QUERIES, args.n) if args.paraphrase
               else [random.choice(SEED_QUERIES) for _ in range(args.n)])

    print(f"Simulating {args.n} requests via {args.mode}...")
    t0 = time.time()
    ok = run_http(queries, args.base_url, args.concurrency) if args.mode == "http" \
        else run_inprocess(queries)
    print(f"\nDone: {ok}/{args.n} succeeded in {time.time()-t0:.1f}s. "
          f"Run `make eval-live` to score this traffic.")


if __name__ == "__main__":
    main()
