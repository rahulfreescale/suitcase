"""Wire the nodes into a LangGraph workflow with the three reflection loops.

    clarify ──> (ask?) ──END
        │
        ▼
    plan ──rag/sql──> researcher ──> plan         (process-reflection loop)
        │
        └reflect──> reflection ──insufficient──> plan   (data-reflection loop)
                        │
                     sufficient
                        ▼
                     writer ──> END               (draft reflection inside writer)

State is checkpointed to Postgres so a failed run resumes from the last node.
"""
from contextlib import contextmanager
from langgraph.graph import StateGraph, START, END
from app.agents.state import AgentState
from app.agents.clarify import clarify_intent
from app.agents.plan import think_and_plan
from app.agents.researcher import researcher
from app.agents.reflection import reflection
from app.agents.writer import writer
from app.config import get_settings

_s = get_settings()


def _after_clarify(state: AgentState) -> str:
    return "ask_user" if state.get("needs_clarification") else "planner"


def _after_plan(state: AgentState) -> str:
    return "research" if state.get("next_action") in ("rag", "sql") else "reflect"


def _after_reflection(state: AgentState) -> str:
    return "write" if state.get("sufficient") else "replan"


def build_graph(checkpointer=None):
    g = StateGraph(AgentState)
    g.add_node("clarify", clarify_intent)
    g.add_node("planner", think_and_plan)
    g.add_node("researcher", researcher)
    g.add_node("reflection", reflection)
    g.add_node("writer", writer)

    g.add_edge(START, "clarify")
    g.add_conditional_edges("clarify", _after_clarify,
                            {"ask_user": END, "planner": "planner"})
    g.add_conditional_edges("planner", _after_plan,
                            {"research": "researcher", "reflect": "reflection"})
    g.add_edge("researcher", "planner")
    g.add_conditional_edges("reflection", _after_reflection,
                            {"replan": "planner", "write": "writer"})
    g.add_edge("writer", END)
    return g.compile(checkpointer=checkpointer)


@contextmanager
def graph_with_checkpointer():
    """Context-managed graph backed by the Postgres checkpointer."""
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        with PostgresSaver.from_conn_string(_s.postgres_dsn) as cp:
            cp.setup()
            yield build_graph(cp)
    except Exception as e:  # fall back to in-memory if Postgres is unavailable
        print(f"[graph] Postgres checkpointer unavailable ({e}); using memory saver")
        from langgraph.checkpoint.memory import MemorySaver
        yield build_graph(MemorySaver())


def run_with_memory(graph, query: str, thread_id: str,
                    session_id: str | None = None, user_id: str | None = None,
                    on_stage=None, on_token=None) -> dict:
    """Invoke the graph with conversation memory loaded in and the turn saved out.

    This is the single place memory is wired to the workflow: load short-term
    (session) + long-term (user) memory, render it into the state so clarify/plan
    can resolve references like "that city", run the graph, then persist this
    completed turn. Memory lives in a SHARED store (Redis), so this same function
    works unchanged when the graph runs inside a distributed worker.

    on_stage: optional callback(node_name) invoked as each graph node completes,
    used to publish live progress events when streaming.
    """
    from app.stores.memory import (
        load_session, load_user_memory, render_memory_context, save_turn,
        extract_and_save_user_facts, resolve_reference)

    session = load_session(session_id)
    user_facts = load_user_memory(user_id)
    memory_context = render_memory_context(session, user_facts)

    # Deterministically resolve back-references ("that city", "there") using
    # session memory BEFORE the model sees the query — but only if enabled.
    # Off by default so a capable model does the resolution from injected memory
    # (more general than regex). Turn on for weak models / guaranteed resolution.
    resolved_query = query
    if _s.memory_resolve_in_code:
        resolved_query = resolve_reference(query, session)

    # Semantic cache: if a similar question was answered recently, return that
    # instantly and skip the whole pipeline. Checked on the RESOLVED query so
    # "that city" -> "Lisbon" is what's matched (context-independent).
    if _s.cache_enabled:
        from app.stores.cache import lookup as cache_lookup
        cached = cache_lookup(resolved_query)
        if cached is not None:
            ans = (cached.get("answer") or "").strip()
            if ans:
                save_turn(session_id, query, ans)   # keep the conversation coherent
            return cached

    _inputs = {"thread_id": thread_id, "query": resolved_query,
               "session_id": session_id or "", "user_id": user_id or "",
               "memory_context": memory_context}
    _cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}
    # Register the token sink out-of-band (keyed by thread_id) so the writer can
    # stream tokens without the (unserializable) callback living in graph state.
    if on_token is not None:
        from app.agents.token_sink import register as _reg
        _reg(thread_id, on_token)
    try:
        if on_stage is not None:
            # Stream node-by-node so we can publish progress as each stage
            # finishes. LangGraph yields {node_name: partial_state} after each
            # node; we keep the latest state and fire the callback per node.
            final = {}
            for update in graph.stream(_inputs, _cfg, stream_mode="updates"):
                for node_name, node_state in update.items():
                    try:
                        on_stage(node_name)
                    except Exception:
                        pass
                    if isinstance(node_state, dict):
                        final.update(node_state)
        else:
            final = graph.invoke(_inputs, _cfg)
    finally:
        if on_token is not None:
            from app.agents.token_sink import clear as _clear
            _clear(thread_id)

    # Persist the turn under the ORIGINAL query (what the user actually typed),
    # so memory reads naturally on replay.
    answer = (final.get("answer") or "").strip()
    if answer:
        save_turn(session_id, query, answer)                        # short-term (fast, no LLM)
        if _s.memory_extract_facts and user_id:
            # Long-term extraction makes an extra LLM call. In a long-running
            # SERVER you'd run this off the response path (background worker /
            # queue). In this in-process flow, do it inline — a spawned daemon
            # thread would be orphaned when the graph context closes. It's cheap
            # (fast model) and never raises (defensive inside).
            extract_and_save_user_facts(user_id, query, answer)
        # Cache grounded answers (not clarifications) on the resolved query.
        if _s.cache_enabled and not final.get("needs_clarification"):
            from app.stores.cache import store as cache_store
            cache_store(resolved_query, {
                "type": "answer", "answer": answer,
                "citations": final.get("citations", []),
                "sources_used": final.get("sources", []),
                "thread_id": thread_id})
    return final
