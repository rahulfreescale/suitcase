"""Show EXACTLY what the agent sends to the LLM and what the LLM requests back.

Prints:
  1. ALL tool schemas sent to the model (name + description + params) — "here
     are all the tools available to you"
  2. The raw request (tools=... passed to litellm)
  3. The model's response — the tool it REQUESTED (name + arguments)

Run from repo root with venv active:
    PYTHONPATH=. python show_tool_call.py
"""
import json
import litellm
from app.agents.tool_agents import TOOL_SCHEMAS
from app.config import get_settings

_s = get_settings()

# ---- 1. ALL TOOLS SENT TO THE MODEL -----------------------------------------
print("=" * 70)
print("STEP 1 — ALL TOOL SCHEMAS SENT TO THE LLM")
print("=" * 70)
print(f"{len(TOOL_SCHEMAS)} tools are offered to the model on every agent call:\n")
for t in TOOL_SCHEMAS:
    fn = t.get("function", t)
    name = fn.get("name")
    desc = (fn.get("description") or "")[:100]
    params = list((fn.get("parameters", {}).get("properties") or {}).keys())
    print(f"  • {name}")
    print(f"      when to use: {desc}...")
    print(f"      params: {params}\n")

# ---- 2. THE REQUEST -----------------------------------------------------------
print("=" * 70)
print("STEP 2 — THE REQUEST (a wheelchair Rome leg)")
print("=" * 70)
messages = [
    {"role": "system", "content":
     "You are a trip-research agent. The traveler uses a wheelchair. You have "
     "tools for weather, routing, rest stops, accessible places and more. "
     "Decide which tool to call for the leg between two Rome stops, then call it."},
    {"role": "user", "content":
     "Leg: Colosseum (41.8902, 12.4922) to Pantheon (41.8986, 12.4768). "
     "Check if a wheelchair user can make this leg and how far it is."},
]
print("messages sent:", json.dumps(messages, indent=1)[:400], "...\n")
print(f"tools sent: all {len(TOOL_SCHEMAS)} schemas above, with tool_choice='auto'")
print("(tool_choice='auto' = the MODEL decides whether/which tool to call)\n")

# ---- 3. THE MODEL'S TOOL REQUEST ---------------------------------------------
print("=" * 70)
print("STEP 3 — WHAT THE MODEL REQUESTED BACK")
print("=" * 70)
model = _s.fast_model if hasattr(_s, "fast_model") else _s.model_chain[0]
resp = litellm.completion(
    model=model, messages=messages, tools=TOOL_SCHEMAS,
    tool_choice="auto", temperature=0, max_tokens=500)

msg = resp["choices"][0]["message"]
tool_calls = msg.get("tool_calls") or []
if tool_calls:
    print(f"The model (unprompted) chose to call {len(tool_calls)} tool(s):\n")
    for tc in tool_calls:
        fn = tc["function"]
        print(f"  → TOOL REQUESTED: {fn['name']}")
        print(f"    arguments: {fn['arguments']}\n")
    print("This is the agentic moment: the model READ the tool descriptions and")
    print("DECIDED which tool fits a wheelchair leg — nobody scripted this choice.")
else:
    print("Model returned text instead of a tool call:")
    print(msg.get("content"))
