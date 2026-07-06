"""The multi-agent dossier graph.

Flow:
    build_itinerary (the constraint-faithful spine; the ONLY placer of activities)
        -> fan out the 5 prose specialists IN PARALLEL
        -> auditor (quality + coherence + accessibility-consistency)
             -> revise?  re-draft only the flagged sections, re-audit (bounded)
             -> approve? -> writer -> END

Boundaries that keep the thesis intact:
  - Only the itinerary node places activities, via the deterministic rater.
  - Specialists draft prose that may only reference placed / accessible places.
  - The auditor rejects any section that recommends a left-out place; an
    accessibility violation forces a revise round.
  - The loop is bounded by max_rounds so it always terminates.

Implemented with LangGraph to match the Ask-side workflow. The specialists run
concurrently via a fan-out/fan-in pattern.
"""
from __future__ import annotations
from typing import TypedDict, Optional
from concurrent.futures import ThreadPoolExecutor

from langgraph.graph import StateGraph, START, END

from app.agents.dossier_specialists import (
    spec_itinerary, PROSE_SPECIALISTS, accessibility_services,
    spec_sense_of_place, spec_logistics, spec_dining, spec_weather, spec_practical,
)
from app.agents.dossier_auditor import audit_sections
from app.agents.dossier_writer import compose_dossier

_SPEC_BY_NAME = {
    "sense_of_place": spec_sense_of_place, "logistics": spec_logistics,
    "weather": spec_weather, "practical": spec_practical,
}


class DossierState(TypedDict, total=False):
    request: str
    user_id: Optional[str]
    use_cache: bool

    contract: dict
    itinerary: dict
    itinerary_meta: dict          # needs_clarification / empty_reason passthrough
    sections: list                # prose specialist outputs
    access_block: dict

    audit: dict
    round: int
    max_rounds: int
    _plan_progress: object

    dossier: dict                 # final output
    halt_reason: Optional[str]    # if the itinerary couldn't be built


# ---- nodes ----------------------------------------------------------------

def n_itinerary(state: DossierState) -> dict:
    spec = spec_itinerary(state["request"], user_id=state.get("user_id"),
                          use_cache=state.get("use_cache", True),
                          progress=state.get("_plan_progress"))
    # if the spine can't be built (needs clarification / empty), halt gracefully
    if spec.get("needs_clarification"):
        return {"halt_reason": "clarify",
                "itinerary_meta": {"clarification_question": spec.get("clarification_question")},
                "contract": spec.get("contract", {})}
    if spec.get("empty_reason"):
        return {"halt_reason": "empty",
                "itinerary_meta": {"empty_reason": spec.get("empty_reason")},
                "contract": spec.get("contract", {})}
    return {"contract": spec.get("contract", {}),
            "itinerary": spec.get("itinerary", {}),
            "access_block": accessibility_services(spec.get("contract", {}))}


def n_specialists(state: DossierState) -> dict:
    """Fan out the prose specialists in parallel, AND run the research agent —
    a true tool-calling agent that decides which enrichment tools this specific
    trip needs and writes a grounded 'Good to Know' section. Its output is added
    to the section list, so it flows through the SAME auditor + revise loop as the
    prose specialists (quality-gated, not special-cased)."""
    if state.get("halt_reason"):
        return {}
    contract, itin, uid = state["contract"], state["itinerary"], state.get("user_id")
    def run(fn): return fn(contract, itin, user_id=uid)
    with ThreadPoolExecutor(max_workers=len(PROSE_SPECIALISTS)) as ex:
        sections = list(ex.map(run, PROSE_SPECIALISTS))

    # Research agent (true agent): model-driven tool selection for THIS trip.
    # Runs on every build; failures are non-fatal (the brief still ships without it).
    try:
        from app.agents.tool_agents import research_agent
        intel = research_agent(contract, itin, user_id=uid)
        if intel and intel.get("body"):
            sections.append(intel)
    except Exception as e:
        print(f"[dossier] research agent skipped: {e}")

    return {"sections": sections, "round": 1}


def n_audit(state: DossierState) -> dict:
    if state.get("halt_reason"):
        return {}
    # Use the VERIFIER AGENT (tool-using fact-checker) when enabled; otherwise
    # the plain text-only auditor. Both return the same verdict shape, so the
    # revise loop is unchanged. Verifier failures fall back to the plain auditor.
    audit = None
    try:
        from app.agents.dossier_verifier import verify_sections, verifier_enabled
        if verifier_enabled():
            audit = verify_sections(state["contract"], state["itinerary"],
                                    state.get("sections", []),
                                    user_id=state.get("user_id"))
            if audit.get("_verifier_error"):
                audit = None  # fall back to plain auditor below
    except Exception as e:
        print(f"[dossier] verifier agent unavailable, using plain auditor: {e}")
        audit = None
    if audit is None:
        audit = audit_sections(state["contract"], state["itinerary"],
                               state.get("sections", []), user_id=state.get("user_id"))
    audit["rounds"] = state.get("round", 1)
    return {"audit": audit}


def n_revise(state: DossierState) -> dict:
    """Re-draft only the sections the auditor flagged, then bump the round."""
    flagged = set(state["audit"].get("sections_to_redraft") or [])
    if not flagged:
        return {"round": state.get("round", 1) + 1}
    contract, itin, uid = state["contract"], state["itinerary"], state.get("user_id")
    sections = list(state.get("sections", []))
    # rebuild flagged sections; pass the auditor's fix hints via a light nudge
    hints = {v["section"]: v.get("fix_hint", "")
             for v in state["audit"].get("violations", []) if v.get("section")}
    for i, s in enumerate(sections):
        name = s.get("section")
        if name in flagged and name in _SPEC_BY_NAME:
            fresh = _SPEC_BY_NAME[name](contract, itin, user_id=uid)
            if hints.get(name):
                fresh["_fix_hint"] = hints[name]
            sections[i] = fresh
        elif name == "trip_intel" and name in flagged:
            # the research agent's section was flagged — re-run the agent so its
            # output goes through the same revise path as the specialists.
            try:
                from app.agents.tool_agents import research_agent
                fresh = research_agent(contract, itin, user_id=uid)
                if fresh and fresh.get("body"):
                    sections[i] = fresh
            except Exception as e:
                print(f"[dossier] research agent revise skipped: {e}")
    return {"sections": sections, "round": state.get("round", 1) + 1}


def n_writer(state: DossierState) -> dict:
    if state.get("halt_reason"):
        # emit a minimal dossier envelope carrying the halt so the API/UI can react
        return {"dossier": {"kind": "dossier", "halt": state["halt_reason"],
                            "itinerary_meta": state.get("itinerary_meta", {}),
                            "contract": state.get("contract", {})}}
    dossier = compose_dossier(
        state["contract"], state["itinerary"], state.get("sections", []),
        state.get("audit", {}), state.get("access_block", {}),
        user_id=state.get("user_id"))
    return {"dossier": dossier}


# ---- edges ----------------------------------------------------------------

def _after_itinerary(state: DossierState) -> str:
    return "halt" if state.get("halt_reason") else "specialists"


def _after_audit(state: DossierState) -> str:
    audit = state.get("audit", {})
    if audit.get("verdict") == "approve":
        return "write"
    if state.get("round", 1) >= state.get("max_rounds", 2):
        return "write"     # bounded: ship the best we have
    return "revise"


def build_dossier_graph():
    g = StateGraph(DossierState)
    g.add_node("plan_spine", n_itinerary)
    g.add_node("specialists", n_specialists)
    g.add_node("review", n_audit)
    g.add_node("revise", n_revise)
    g.add_node("writer", n_writer)

    g.add_edge(START, "plan_spine")
    g.add_conditional_edges("plan_spine", _after_itinerary,
                            {"halt": "writer", "specialists": "specialists"})
    g.add_edge("specialists", "review")
    g.add_conditional_edges("review", _after_audit,
                            {"revise": "revise", "write": "writer"})
    g.add_edge("revise", "review")
    g.add_edge("writer", END)
    return g.compile()


_GRAPH = None

def build_dossier(request: str, user_id: str | None = None,
                  use_cache: bool = True, max_rounds: int = 1) -> dict:
    """Run the full multi-agent dossier pipeline and return the dossier object."""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_dossier_graph()
    final = _GRAPH.invoke({"request": request, "user_id": user_id,
                           "use_cache": use_cache, "max_rounds": max_rounds,
                           "round": 1})
    return final.get("dossier", {})


# human-friendly labels + ordering for the live progress tracker
_STAGE_LABELS = {
    "plan_spine": "Building your itinerary",
    "specialists": "Researching (4 specialists + research agent)",
    "review": "Auditing for quality & access",
    "revise": "Refining flagged sections",
    "writer": "Composing your Travel Brief",
}
_STAGE_ORDER = ["plan_spine", "specialists", "review", "writer"]


def build_dossier_stream(request: str, user_id: str | None = None,
                         use_cache: bool = True, max_rounds: int = 1):
    """Generator: run the pipeline, yielding progress as each agent completes AND
    sub-steps as the itinerary spine works through its phases (extract, retrieve,
    rate, assemble, meals), then the composed dossier. Powers the live tracker.
    """
    import queue as _queue
    import threading

    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_dossier_graph()

    q = _queue.Queue()
    SENTINEL = object()

    # the plan spine pushes fine-grained sub-progress here
    def _plan_progress(stage, label):
        q.put({"type": "substep", "stage": stage, "label": label})

    # patch the progress callback into the itinerary spine via state
    def _run():
        done = []
        final_state = {}
        try:
            for event in _GRAPH.stream(
                    {"request": request, "user_id": user_id, "use_cache": use_cache,
                     "max_rounds": max_rounds, "round": 1,
                     "_plan_progress": _plan_progress}):
                for node_name, delta in event.items():
                    if node_name not in done:
                        done.append(node_name)
                    if isinstance(delta, dict):
                        final_state.update(delta)
                    q.put({"type": "progress", "stage": node_name,
                           "label": _STAGE_LABELS.get(node_name, node_name),
                           "done": [s for s in _STAGE_ORDER if s in done],
                           "order": _STAGE_ORDER})
            q.put({"type": "done", "dossier": final_state.get("dossier", {})})
        except Exception as e:
            q.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            q.put(SENTINEL)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    while True:
        item = q.get()
        if item is SENTINEL:
            break
        yield item
