"""Trace both agents on a real example — prints each agent call, every tool the
model chose, the loop iterations, and the grounded output.

Run from repo root with venv active:
    python trace_agents.py                 # default: wheelchair Rome + new-city onboarding
    python trace_agents.py Tokyo toddler   # try another city/constraint

This is the honest "show me the agents working" artifact — it instruments the
real agents (not a mock) and reports the actual tool-selection loop.
"""
import sys
from app.agents.tool_agents import research_agent, onboarding_agent


def _banner(t):
    print("\n" + "=" * 70)
    print(t)
    print("=" * 70)


def trace_research(city, constraint):
    _banner(f"RESEARCH AGENT  —  {city} · {constraint}")

    # a minimal 2-stop itinerary so the agent has real coordinates to reason about
    contract = {"destination": city, "travelers": []}
    if constraint == "wheelchair":
        contract["travelers"] = [{"type": "adult", "mobility": "wheelchair"}]
    elif constraint == "toddler":
        contract["travelers"] = [{"type": "toddler", "mobility": None}]
    elif constraint == "senior":
        contract["travelers"] = [{"type": "senior", "mobility": None}]

    # representative coordinates for two famous stops (Rome defaults)
    stops = {
        "Rome": [("Colosseum", 41.8902, 12.4922), ("Pantheon", 41.8986, 12.4768)],
        "Tokyo": [("Senso-ji", 35.7148, 139.7967), ("Ueno Park", 35.7156, 139.7745)],
        "Paris": [("Louvre", 48.8606, 2.3376), ("Eiffel Tower", 48.8584, 2.2945)],
    }.get(city, [("City Center", 41.9, 12.5), ("Old Town", 41.89, 12.49)])

    itinerary = {"days": [{"day": 1, "blocks": {
        "morning": {"name_hint": stops[0][0], "lat": stops[0][1], "lng": stops[0][2]},
        "afternoon": {"name_hint": stops[1][0], "lat": stops[1][1], "lng": stops[1][2]},
    }}]}

    step = {"n": 0}
    def on_step(kind, name, payload):
        # chat_tools fires on_step("tool_call", tool_name, args) per tool call
        step["n"] += 1
        print(f"  step {step['n']:>2} · MODEL CALLED TOOL: {name}  args={payload}")

    print(f"contract   : {contract}")
    print(f"itinerary  : {stops[0][0]} -> {stops[1][0]}")
    print("\n-- the loop (model decides each step) --")
    out = research_agent(contract, itinerary, on_step=on_step)

    print(f"\n-- TOOL TRACE (what the model actually called) --")
    for i, t in enumerate(out.get("trace", []), 1):
        print(f"  {i}. {t.get('name')}({t.get('args')})")
    print(f"\n  total iterations : {out.get('iters')}")
    print(f"  tools called     : {len(out.get('trace', []))}")
    print(f"\n-- GROUNDED OUTPUT ('Good to Know' section) --\n")
    print(out.get("body", "(no body)"))


def trace_onboarding():
    _banner("ONBOARDING AGENT  —  new city (Meerut), evidence-before-rating")

    # candidate places the LLM would propose for an out-of-corpus city
    candidates = [
        {"name": "Meerut Clock Tower", "lat": 28.9845, "lng": 77.7064},
        {"name": "Suraj Kund Park", "lat": 28.9931, "lng": 77.7010},
    ]
    print(f"candidate places: {[c['name'] for c in candidates]}")

    step = {"n": 0}
    def on_step(kind, name, payload):
        step["n"] += 1
        print(f"  step {step['n']:>2} · MODEL CALLED TOOL: {name}  args={payload}")

    print("\n-- the loop (agent investigates each place before rating) --")
    out = onboarding_agent("Meerut", candidates, on_step=on_step)

    print(f"\n-- TOOL TRACE --")
    for i, t in enumerate(out.get("trace", []), 1):
        print(f"  {i}. {t.get('name')}({t.get('args')})")
    print(f"\n-- EVIDENCE-BASED VERDICTS (what it recorded) --")
    for r in out.get("ratings", []):
        print(f"  {r.get('place')}: wheelchair={r.get('wheelchair')} "
              f"confidence={r.get('confidence')}  note={r.get('note','')[:80]}")


if __name__ == "__main__":
    city = sys.argv[1] if len(sys.argv) > 1 else "Rome"
    constraint = sys.argv[2] if len(sys.argv) > 2 else "wheelchair"
    trace_research(city, constraint)
    trace_onboarding()
    print("\n" + "=" * 70)
    print("DONE — this is the real agent behaviour, not a mock.")
    print("=" * 70)
