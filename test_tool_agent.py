"""Standalone test: does the tool-calling loop actually work?
Run from repo root with venv active:  python test_tool_agent.py
"""
from app.agents.tool_agents import research_agent

# a wheelchair Rome trip with real coords
contract = {"destination": "Rome",
            "travelers": [{"type": "adult", "mobility": "wheelchair"}]}
itinerary = {"days": [
    {"day": 1, "blocks": {
        "morning": {"name_hint": "Colosseum", "lat": 41.8902, "lng": 12.4922},
        "afternoon": {"name_hint": "Pantheon", "lat": 41.8986, "lng": 12.4768},
        "evening": {"name_hint": "Piazza Navona", "lat": 41.8992, "lng": 12.4731}}},
]}

def show(kind, name, payload):
    if kind == "tool_call":
        print(f"  🔧 MODEL CHOSE: {name}({payload})")
    else:
        r = str(payload)[:80]
        print(f"     ↳ returned: {r}")

print("=== Running research agent (watch which tools the MODEL decides to call) ===\n")
out = research_agent(contract, itinerary, on_step=show)
print(f"\n=== Agent made {len(out.get('trace',[]))} tool calls over {out.get('iters',0)} iterations ===")
print("\n--- The 'Good to Know' section it wrote: ---")
print(out.get("body") or out.get("error"))
print("\n--- Tools it chose (the agentic decision) ---")
for t in out.get("trace", []):
    print(f"  • {t['name']}({t['args']})")
