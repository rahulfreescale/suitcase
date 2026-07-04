"""Demo: model gateway routing (no experiment running).

Shows which model chain the gateway routes each task to. A/B assignment is
demonstrated separately in eval/simulate_ab.py (which uses the Redis-backed
experiment store).

    python -m eval.demo_gateway
"""
from __future__ import annotations
from app import gateway


def main():
    print("=== ROUTING (task -> chosen model chain, no experiment running) ===")
    for task in ["clarify", "plan", "reflect", "research", "write"]:
        chain, variant, payload = gateway._resolve(task, user_id=None)
        print(f"{task:10s} -> {chain[0]:55s}  (chain of {len(chain)})")
    print("\nCheap tasks route to the fast model; research/write use the strong chain.")
    print("Run eval/simulate_ab.py to see A/B variant assignment.")


if __name__ == "__main__":
    main()
