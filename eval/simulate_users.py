"""Emulate production traffic: several users, each with a multi-turn session.

This is the "what does production look like" simulator. It generates a realistic
stream where:
  - multiple USERS each run a short SESSION of 2-4 related questions,
  - every request is one Langfuse trace tagged with session_id + user_id,
  - all traffic is tagged environment="simulate" so it's separable from real
    traffic and from eval runs.

After running, open Langfuse and you can:
  - filter Environment = simulate,
  - group/track metrics per user_id or per session,
  - build dashboards (latency, cost, volume) and set alerts.

Usage:
  python -m eval.simulate_users --users 5 --per-user 3      # ~15 requests
  python -m eval.simulate_users --users 4 --per-user 4 --sleep 2
"""
from __future__ import annotations
import argparse
import random
import time
import uuid

# Tag ALL traffic from this process as environment="simulate" BEFORE app imports,
# so it's cleanly separable from real traffic (default) and eval (eval).
import os
os.environ.setdefault("LANGFUSE_TRACING_ENVIRONMENT", "simulate")

from app.agents.graph import graph_with_checkpointer
from app.observability import request_trace, current_trace_id, flush as lf_flush

# Realistic user "personas": each tends to ask a themed cluster of questions,
# which makes per-session traces read like a coherent conversation.
PERSONAS = {
    "tox-reviewer": [
        "Were piloerection and ataxia observed in study T123456-2?",
        "What clinical findings were seen at the high dose in study T123456-2?",
        "What was the NOAEL in that study?",
        "Were loose faeces recorded in study T123456-2?",
    ],
    "data-analyst": [
        "How many studies were done on rats?",
        "Which studies lasted longer than 30 days?",
        "List all oral studies on compound BAY-1 with their doses.",
        "What species were used across the studies?",
    ],
    "safety-lead": [
        "Summarize the cardiovascular effects of BAY-7 in dogs.",
        "Did study T200110-4 show sustained cardiovascular effects?",
        "Any safety concerns with BAY-1?",
        "Across all rat studies, were any neurological signs reported?",
    ],
    "curious-newcomer": [
        "Tell me about the findings.",                 # vague -> clarify
        "What is a NOAEL?",                            # may be unanswerable from data
        "Compare the findings between T123456-2 and T123457-9.",
        "What's the newest study you have?",
    ],
}


def run_session(graph, user_id: str, persona: str, n: int, sleep_s: float):
    """One user's session: n related questions sharing a session_id."""
    session_id = f"sess-{persona}-{uuid.uuid4().hex[:6]}"
    questions = random.sample(PERSONAS[persona], min(n, len(PERSONAS[persona])))
    print(f"\n[user={user_id} persona={persona} session={session_id}] {len(questions)} turns")
    for i, q in enumerate(questions, 1):
        tid = str(uuid.uuid4())
        t0 = time.time()
        with request_trace("ask", q, session_id=session_id, user_id=user_id,
                            tags=[persona]):
            final = graph.invoke(
                {"thread_id": tid, "query": q},
                {"configurable": {"thread_id": tid}, "recursion_limit": 40})
            _ = current_trace_id()
        dt = time.time() - t0
        ans = (final.get("answer", "") or "").strip().replace("\n", " ")
        print(f"  [{i}/{len(questions)}] {dt:4.1f}s  Q: {q[:48]:48}  A: {ans[:60]}")
        if sleep_s:
            time.sleep(sleep_s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=5, help="number of distinct users")
    ap.add_argument("--per-user", type=int, default=3, help="requests per user session")
    ap.add_argument("--sleep", type=float, default=1.0, help="seconds between requests (rate-limit pacing)")
    args = ap.parse_args()

    personas = list(PERSONAS.keys())
    total = 0
    with graph_with_checkpointer() as graph:
        for u in range(1, args.users + 1):
            user_id = f"user_{u:03d}"
            persona = personas[(u - 1) % len(personas)]
            run_session(graph, user_id, persona, args.per_user, args.sleep)
            total += min(args.per_user, len(PERSONAS[persona]))
    lf_flush()
    print(f"\nDone. Simulated ~{total} requests across {args.users} users "
          f"(environment=simulate). Open Langfuse -> filter Environment=simulate, "
          f"then group by user_id / session_id.")


if __name__ == "__main__":
    main()
