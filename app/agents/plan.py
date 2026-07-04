"""Node 2 - Think & Plan: PROCESS reflection. Pick the next action or stop."""
import json
from app.agents.state import AgentState
from app import gateway
from app.observability import observe
from app.config import get_settings
from app.stores.appstate_dynamo import log_step

_s = get_settings()
_PROMPT = """You are the planner for a research agent. Reason about progress toward
the user's goal (process reflection), then choose the NEXT action.

Question: {q}
Allowed sources: {sources}
Open follow-up questions: {followups}
Evidence gathered so far ({n} items): {summary}

Choose next_action:
- "rag"     to retrieve from narrative reports
- "sql"     to query structured metadata
- "reflect" if you believe enough has been gathered to evaluate sufficiency
Return ONLY JSON: {{"reasoning": "...", "next_action": "rag|sql|reflect"}}"""


@observe(name="think_and_plan")
def think_and_plan(state: AgentState) -> AgentState:
    ev = state.get("evidence", [])
    summary = json.dumps([{"tool": e.get("tool"), "found": e.get("found")} for e in ev])
    try:
        out = gateway.chat_json("plan", [{"role": "user", "content": _PROMPT.format(
            q=state["clarified_query"], sources=state["sources"],
            followups=state.get("follow_ups", []), n=len(ev), summary=summary)}],
            user_id=state.get("user_id"))
        action = out.get("next_action", "reflect")
        reasoning = out.get("reasoning", "")
    except Exception:
        action, reasoning = ("reflect" if ev else "rag"), "fallback"

    # Guardrails: respect allowed sources and the research-loop cap.
    if action in ("rag", "sql") and action not in state["sources"]:
        action = "reflect"
    if state.get("research_loops", 0) >= _s.max_research_loops:
        action = "reflect"

    log_step(state["thread_id"], f"02_plan_{state.get('research_loops',0)}",
             {"node": "plan", "next_action": action, "reasoning": reasoning})
    return {**state, "plan": reasoning, "next_action": action}
