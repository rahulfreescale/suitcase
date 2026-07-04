"""Simulate A/B experiments — dry-run (free, plumbing only) or real (fires traffic).

Two scenarios, run one at a time:
  1. MODEL:  write step uses strong (A) vs fast/cheap (B) model  — cost experiment
  2. PROMPT: write step uses default (A) vs concise (B) prompt   — style experiment

DRY RUN (default) — no LLM calls. Sets the experiment in Redis, assigns N users,
prints the A/B split and each user's variant. Verifies the plumbing for free:
  python -m eval.simulate_ab --scenario model
  python -m eval.simulate_ab --scenario prompt

REAL — fires actual requests through /ask_stream so traces get tagged in Langfuse:
  python -m eval.simulate_ab --scenario model --real --users 30 --questions 3

After a real run, compare variants in Langfuse by the variant:A / variant:B tags.
Kill an experiment any time:  curl -X DELETE localhost:8080/admin/experiment/write-experiment
"""
from __future__ import annotations
import argparse, json, time, urllib.request
from app.config import get_settings
from app import experiments

_s = get_settings()
BASE = "http://localhost:8080"

QUESTIONS = [
    "What clinical signs were seen in study T123456-2?",
    "What was the NOAEL in study T123456-2?",
    "Were there liver findings in study T123456-2?",
    "Summarize the kidney findings in study T200110-4.",
    "What histopathology was reported for study T200110-4?",
]


def scenario_config(scenario: str) -> dict:
    strong = _s.model_chain
    fast = [_s.llm_fast_model] + _s.model_chain
    if scenario == "model":
        return {"A": {"share": 50, "payload": {"model_chain": strong}},
                "B": {"share": 50, "payload": {"model_chain": fast}}}
    if scenario == "prompt":
        return {"A": {"share": 50, "payload": {}},
                "B": {"share": 50, "payload": {"prompt_key": "concise"}}}
    raise SystemExit("scenario must be 'model' or 'prompt'")


def dry_run(scenario: str, n_users: int):
    variants = scenario_config(scenario)
    experiments.set_experiment("write-experiment", "write", variants)
    print(f"=== DRY RUN · scenario={scenario} · {n_users} users ===")
    print(f"experiment set in Redis: {json.dumps(variants)[:90]}...\n")
    counts = {}
    for i in range(n_users):
        uid = f"user_{i}"
        v, payload = experiments.get_variant("write-experiment", uid)
        counts[v] = counts.get(v, 0) + 1
        tag = (payload.get("model_chain", [""])[0] if scenario == "model"
               else payload.get("prompt_key", "default"))
        print(f"  {uid:9s} -> variant {v}  ({tag})")
    print(f"\nsplit: {counts}")
    # stability
    a, _ = experiments.get_variant("write-experiment", "user_5")
    b, _ = experiments.get_variant("write-experiment", "user_5")
    print(f"stability: user_5 twice -> {a}, {b} (must match)")
    print("\nplumbing verified. Re-run with --real to fire actual tagged traffic.")
    experiments.stop_experiment("write-experiment")
    print("experiment cleared (dry run leaves nothing running).")


def _ask(q, uid):
    # Append a per-user marker so each question is UNIQUE -> always a cache miss,
    # forcing the full pipeline (and the write-task model) to actually run. Without
    # this, repeated questions hit the semantic cache and skip the model entirely,
    # which would confound a model A/B (a cache hit uses neither variant's model).
    q = f"{q} (ref {uid})"
    body = json.dumps({"query": q, "session_id": f"sess-{uid}", "user_id": uid}).encode()
    req = urllib.request.Request(f"{BASE}/ask_stream", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    got = None
    with urllib.request.urlopen(req) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if line.startswith("data:"):
                ev = json.loads(line[5:].strip())
                if ev.get("type") == "done":
                    got = ev.get("latency_s")
    return got


def real_run(scenario: str, n_users: int, n_q: int):
    variants = scenario_config(scenario)
    # start it via the admin endpoint (the real control path)
    body = json.dumps({"name": "write-experiment", "task": "write",
                       "variants": variants}).encode()
    req = urllib.request.Request(f"{BASE}/admin/experiment", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req).read()
    print(f"=== REAL · scenario={scenario} · {n_users} users × {n_q} q ===")
    print("experiment started via /admin/experiment. Firing traffic...\n")
    counts = {}
    for i in range(n_users):
        uid = f"user_{i}"
        v, _ = experiments.get_variant("write-experiment", uid)
        counts[v] = counts.get(v, 0) + 1
        for j in range(n_q):
            q = QUESTIONS[(i + j) % len(QUESTIONS)]
            lat = _ask(q, uid)
            print(f"  {uid:9s} [{v}] q{j+1} -> {lat}s")
    print(f"\nsplit: {counts}")
    print("Done. Compare variant:A vs variant:B in Langfuse (cost/latency/scores).")
    print("Kill when done: curl -X DELETE localhost:8080/admin/experiment/write-experiment")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=["model", "prompt"], default="model")
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--users", type=int, default=30)
    ap.add_argument("--questions", type=int, default=3)
    a = ap.parse_args()
    if a.real:
        real_run(a.scenario, a.users, a.questions)
    else:
        dry_run(a.scenario, a.users)
