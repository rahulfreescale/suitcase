"""Shared state for the multi-agent plan-review graph.

The review loop runs AFTER the deterministic pipeline has produced a first-draft
itinerary (extract -> retrieve -> RATE -> assemble). A panel of critic agents
evaluates the draft from four angles, an arbiter merges their critiques, and a
reviser proposes swaps - but only among places that already passed the
deterministic hard-constraint rater. The loop is bounded.

Design invariant: nothing in this graph may place a hard-FAIL activity. Critics
only emit critiques; the reviser may only shuffle constraint-passing places and
must call the rater tool for anything new. Autonomy lives here; the guarantee
lives in code.
"""
from typing import TypedDict, Optional


class PlanReviewState(TypedDict, total=False):
    # inputs (from the deterministic first draft)
    request: str
    contract: dict                  # the constraint contract
    user_id: Optional[str]

    itinerary: dict                 # current draft: {days:[{day,blocks:{...}}], skipped:[...]}
    bench: list                     # constraint-PASSING places not currently placed
                                    # (the reviser's legal swap pool)

    # critic outputs (one entry per critic, merged by the arbiter)
    critiques: list                 # [{critic, severity, issues:[...], asks:[...]}]
    arbiter_summary: dict           # {top_issues:[...], want_retrieval:bool, missing:[...]}

    # loop control
    round: int
    max_rounds: int
    satisfied: bool                 # arbiter's verdict: is the plan good enough?
    revision_log: list              # human-readable trace of what changed each round

    # retrieval gate (critics can ask for fresh candidates if the bench is thin)
    want_retrieval: bool
    retrieval_hint: list            # e.g. ["gardens", "quieter evening spot"]
