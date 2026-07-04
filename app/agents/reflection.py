"""Node 4 - Reflection: DATA reflection. Is the evidence sufficient?"""
import json
from app.agents.state import AgentState
from app import gateway
from app.observability import observe
from app.config import get_settings
from app.stores.appstate_dynamo import log_step

_s = get_settings()
_PROMPT = """Assess whether the gathered evidence is sufficient and relevant to
answer the question. If not, list specific follow-up questions to fill the gaps.

Question: {q}
Evidence: {evidence}

Return ONLY JSON: {{"sufficient": true|false, "follow_ups": ["...", ...]}}"""


@observe(name="reflection")
def reflection(state: AgentState) -> AgentState:
    ev = state.get("evidence", [])
    ev_view = json.dumps([
        {"tool": e.get("tool"),
         "context": (e.get("context") or "")[:800],
         "rows": (e.get("rows") or [])[:8]} for e in ev])
    try:
        out = gateway.chat_json("reflect", [{"role": "user", "content": _PROMPT.format(
            q=state["clarified_query"], evidence=ev_view)}],
            user_id=state.get("user_id"))
        sufficient = bool(out.get("sufficient"))
        follow_ups = [str(x) for x in out.get("follow_ups", [])][:3]
    except Exception:
        sufficient, follow_ups = True, []

    loops = state.get("reflection_loops", 0) + 1
    if loops > _s.max_reflection_loops:   # stop looping; answer with what we have
        sufficient = True
    if not any(e.get("found") for e in ev):  # nothing found -> stop, be honest
        sufficient = True

    log_step(state["thread_id"], f"04_reflect_{loops}",
             {"node": "reflection", "sufficient": sufficient, "follow_ups": follow_ups})
    return {**state, "sufficient": sufficient, "follow_ups": follow_ups,
            "reflection_loops": loops}
