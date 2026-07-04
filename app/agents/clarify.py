"""Node 1 - Clarify intent: fail-fast on ambiguity, else recommend sources."""
from app.agents.state import AgentState
from app import gateway
from app.observability import observe
from app.stores.appstate_dynamo import log_step

_PROMPT = """A user asks a travel-planning assistant a question. Decide:
- sources: which of ["rag","sql"] are relevant (rag = destination guides (prose),
  sql = structured flights & stays tables). Pick one or both.
- needs_clarification: true ONLY if the question is genuinely too ambiguous to route.
- clarification_question: if needed, one concise question; else null.
- clarified_query: a lightly enriched, SELF-CONTAINED version of the question.

CRITICAL — using conversation memory:
If a "Conversation memory" block is present below, you MUST use it to resolve
references. Specifically:
- If the question contains a reference like "that study", "it", "there", "the
  same one", "that city", find the concrete referent (e.g. a city like
  Lisbon or Tokyo) in the memory and SUBSTITUTE it into
  clarified_query so the query stands alone.
- When memory lets you resolve the referent, you MUST set needs_clarification to
  false and clarification_question to null. Do NOT ask the user which city/
  destination they mean if the memory already tells you.
- Only set needs_clarification true if the reference cannot be resolved from
  memory AND the question is otherwise unroutable.

Return ONLY JSON with keys: sources, needs_clarification, clarification_question, clarified_query.

{mem}Current question: {q}"""


@observe(name="clarify_intent")
def clarify_intent(state: AgentState) -> AgentState:
    q = state["query"]
    mem = state.get("memory_context", "")
    try:
        out = gateway.chat_json("clarify", [{"role": "user", "content": _PROMPT.format(mem=mem, q=q)}], user_id=state.get("user_id"))
    except Exception:
        out = {"sources": ["rag"], "needs_clarification": False,
               "clarification_question": None, "clarified_query": q}
    sources = [s for s in out.get("sources", ["rag"]) if s in ("rag", "sql")] or ["rag"]
    log_step(state["thread_id"], "01_clarify",
             {"node": "clarify", "sources": sources,
              "needs_clarification": out.get("needs_clarification", False)})
    return {**state, "sources": sources,
            "needs_clarification": bool(out.get("needs_clarification")),
            "clarification_question": out.get("clarification_question"),
            "clarified_query": out.get("clarified_query", q),
            "evidence": [], "citations": [], "follow_ups": [],
            "research_loops": 0, "reflection_loops": 0, "sufficient": False}
