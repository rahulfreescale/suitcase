"""Demo: conversation memory in action — short-term AND long-term.

SHORT-TERM: within one session, a reference ("that study") resolves to an
earlier turn.

LONG-TERM: across two SEPARATE sessions for the same user, durable facts learned
in session 1 (e.g. "focuses on study T123456-2 / BAY-1") are remembered and shown
at the start of session 2.

Usage:
  make up                       # ensure redis + core services are running
  LLM_MODEL_CHAIN="anthropic/claude-haiku-4-5,gpt-4o-mini" python -m eval.demo_memory
"""
from __future__ import annotations
import os
import uuid

os.environ.setdefault("LANGFUSE_TRACING_ENVIRONMENT", "memory-demo")

from app.agents.graph import graph_with_checkpointer, run_with_memory
from app.stores.memory import load_user_memory
from app.observability import request_trace, flush as lf_flush

USER = "user_memdemo"

# Session 1 — establishes context AND reveals facts about the USER (their role /
# focus), so long-term extraction has real user-signal to catch — not just study
# data. The first turn states who the user is; that's what becomes a durable fact.
SESSION_1 = f"sess-memdemo-{uuid.uuid4().hex[:6]}"
TURNS_1 = [
    "I'm the lead tox reviewer on the BAY-1 program. Were piloerection and ataxia observed in study T123456-2?",
    "What clinical findings were seen at the high dose in that study?",  # "that study" (short-term)
    "And were loose faeces recorded there?",                            # "there" (short-term)
]

# Session 2 — a fresh session (new session_id), SAME user. Tests long-term memory.
# These are phrased so the user's known focus (BAY-1 / T123456-2) should fill the gap.
SESSION_2 = f"sess-memdemo-{uuid.uuid4().hex[:6]}"
TURNS_2 = [
    "Remind me what the main clinical signs were.",        # "the" signs — from their focus study
    "Was there a NOAEL established?",                       # NOAEL for — their study, implicitly
]


def run_session(graph, session_id, turns, label):
    print(f"\n{'='*72}\n{label}  session={session_id}  user={USER}\n{'='*72}")
    for i, q in enumerate(turns, 1):
        tid = str(uuid.uuid4())
        with request_trace("ask", q, session_id=session_id, user_id=USER,
                           tags=["memory-demo"]):
            final = run_with_memory(graph, q, tid, session_id=session_id, user_id=USER)
        ans = (final.get("answer") or "").strip()
        asked = final.get("clarification_question")
        print(f"\nTurn {i}: {q}")
        if asked and not ans:
            print(f"  -> CLARIFY ASKED (memory did NOT resolve it): {asked}")
        else:
            print(f"  -> ANSWERED: {ans[:200]}")


def main():
    with graph_with_checkpointer() as graph:
        # --- SHORT-TERM: within session 1 ---
        run_session(graph, SESSION_1, TURNS_1, "SHORT-TERM (references resolve within a session)")

        # Show what long-term facts were learned about the user (extraction runs
        # inline during each turn, so they're already saved by now).
        facts = load_user_memory(USER)
        print(f"\n{'-'*72}\nLONG-TERM facts now known about {USER}:")
        if facts:
            for f in facts:
                print(f"  • {f}")
        else:
            print("  (none extracted — check that memory_extract_facts is on and Redis is up)")

        # --- LONG-TERM: a brand-new session, same user ---
        run_session(graph, SESSION_2, TURNS_2, "LONG-TERM (new session, same user — facts carry over)")

    lf_flush()
    print(f"\n{'='*72}")
    print("Short-term worked if turns 2-3 in session 1 ANSWERED (not asked which study).")
    print("Long-term worked if facts were extracted above AND the session-2 answer")
    print("reflects this user's focus (e.g. BAY-1 / T123456-2) without you restating it.")


if __name__ == "__main__":
    main()
