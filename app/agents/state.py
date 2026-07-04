"""Shared workflow state. LangGraph persists this per thread via the checkpointer."""
from typing import TypedDict, Optional


class AgentState(TypedDict, total=False):
    thread_id: str
    session_id: str                 # groups turns of one conversation (memory key)
    user_id: str                    # attributes turns to a user (long-term memory key)
    memory_context: str             # rendered short+long-term memory, injected into prompts
    query: str                      # original user question
    clarified_query: str            # enriched after clarify
    sources: list[str]              # selected data sources (rag / sql)
    needs_clarification: bool
    clarification_question: Optional[str]

    # --- constraint contract (Extract Requirements agent) ---
    constraints: dict               # typed contract: destination, travelers, budget, dietary, ...
    detected_constraints: list[str] # supported constraints found (green chips)
    suggested_constraints: list[str] # supported constraints NOT found (+ add chips)

    plan: str                       # latest Think&Plan decision
    next_action: str                # "rag" | "sql" | "reflect"
    follow_ups: list[str]           # from data reflection

    evidence: list[dict]            # accumulated tool outputs
    citations: list[dict]

    research_loops: int
    reflection_loops: int
    sufficient: bool
    grounded: bool                  # did retrieval clear the relevance gate?

    answer: str
