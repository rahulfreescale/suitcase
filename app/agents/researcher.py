"""Node 3 - Researcher: execute the planned tool (RAG and/or Text-to-SQL)."""
from app.agents.state import AgentState
from app.tools.rag_tool import run_rag
from app.tools.sql_tool import run_sql
from app.observability import observe
from app.stores.appstate_dynamo import log_step


@observe(name="researcher")
def researcher(state: AgentState) -> AgentState:
    action = state.get("next_action")
    q = state["clarified_query"]
    if state.get("follow_ups"):
        q = q + " | follow-up: " + "; ".join(state["follow_ups"])

    result = run_sql(q) if action == "sql" else run_rag(q)
    evidence = state.get("evidence", []) + [result]
    citations = state.get("citations", []) + result.get("citations", [])

    # Carry a grounding signal forward. RAG sets found=False when the best chunk
    # falls below the relevance threshold; SQL grounding is the presence of rows.
    grounded = state.get("grounded", False) or bool(result.get("found"))

    log_step(state["thread_id"],
             f"03_research_{state.get('research_loops',0)}",
             {"node": "researcher", "tool": result.get("tool"),
              "found": result.get("found"), "trace": result.get("trace")})
    return {**state, "evidence": evidence, "citations": citations,
            "grounded": grounded,
            "research_loops": state.get("research_loops", 0) + 1,
            "follow_ups": []}
