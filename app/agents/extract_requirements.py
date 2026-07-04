"""Node - Extract Requirements: turn a free-text trip request into a typed
constraint contract.

This is the constraint-detector. Its sole job is: request -> structured contract
of what the traveler needs. Everything downstream (rating, critic, skipped-list)
reads from this contract, and the UI renders it as detected + suggested chips.

Design (locked):
- Hard vs soft is NOT decided here. We only CAPTURE requirements (preserving
  phrasing, esp. for dietary, so Phase 2 can classify medical-vs-preference).
- We also emit `suggested` - the requirements the user did NOT mention but that
  the system supports - so the UI can offer "+ add" chips and teach what's
  available.
- Missing REQUIRED fields (destination, trip length) set needs_clarification so
  the existing Clarify node can ask. (Reuse, per design.)
"""
from app.agents.state import AgentState
from app import gateway
from app.observability import observe
from app.stores.appstate_dynamo import log_step

# The full menu of constraints the system understands. Anything the user didn't
# mention becomes a "+ add" suggestion chip. Keep in sync with the UI mockup.
_SUPPORTED = [
    "wheelchair",        # step-free / no-stairs access  (hard, decided later)
    "toddler",           # toddler / young-child friendly (soft)
    "senior",            # limited-mobility older traveler (soft)
    "stroller",          # traveling with a stroller (soft)
    "budget",            # a spend limit (hard, decided later)
    "dietary",           # food need/preference (soft OR hard by phrasing)
    "trip_length",       # number of days
    "pace",              # relaxed vs packed
]

_PROMPT = """You extract a structured TRIP REQUIREMENTS CONTRACT from a user's
travel-planning request. Return ONLY JSON. Do not plan the trip.

Extract these fields:
- destination: the city, exactly as a proper name (e.g. "Prague"), or null if none stated.
- trip_length_days: integer number of days if stated, else null.
- travelers: list of objects, each {{"type": <"adult"|"toddler"|"child"|"senior">,
  "mobility": <"wheelchair"|"stroller"|null>}}. Infer sensibly:
  "someone in a wheelchair" -> {{"type":"adult","mobility":"wheelchair"}};
  "a toddler" -> {{"type":"toddler","mobility":null}}.
- budget: object {{"amount": <number>, "unit": <"per_night"|"per_day"|"total"|"unknown">}}
  or null if no budget stated.
- dietary: list of objects, each {{"need": <short label e.g. "vegetarian",
  "nut allergy", "gluten-free">, "phrasing": <the user's exact words for it>,
  "medical": <true if stated as an allergy/medical/strict religious need like
  celiac, severe allergy, kosher, halal; false if a preference like "we prefer
  vegetarian">}}. Empty list if none.
- other: list of short free-text requirements that don't fit above (e.g.
  "wants museums", "romantic", "near the water"). Empty list if none.

Rules:
- Capture, do not classify hard/soft. Preserve dietary "phrasing" verbatim.
- Do NOT invent requirements the user didn't state.
- destination and trip_length_days are REQUIRED for planning. If EITHER is
  missing, set needs_more=true and missing_required=[the missing field name(s)].
  Otherwise needs_more=false and missing_required=[].

Return ONLY JSON with keys: destination, trip_length_days, travelers, budget,
dietary, other, needs_more, missing_required.

Request: {q}"""


def _detected_labels(contract: dict) -> list[str]:
    """Which supported constraints did we actually detect? (for chips)"""
    found = []
    trav = contract.get("travelers") or []
    if any((t or {}).get("mobility") == "wheelchair" for t in trav):
        found.append("wheelchair")
    if any((t or {}).get("mobility") == "stroller" for t in trav):
        found.append("stroller")
    if any((t or {}).get("type") == "toddler" for t in trav):
        found.append("toddler")
    if any((t or {}).get("type") == "senior" for t in trav):
        found.append("senior")
    if contract.get("budget"):
        found.append("budget")
    if contract.get("dietary"):
        found.append("dietary")
    if contract.get("trip_length_days") is not None:
        found.append("trip_length")
    return found


@observe(name="extract_requirements")
def extract_requirements(state: AgentState) -> AgentState:
    q = state.get("clarified_query") or state["query"]
    try:
        out = gateway.chat_json(
            "clarify",  # reuse the clarify task's model routing
            [{"role": "user", "content": _PROMPT.format(q=q)}],
            user_id=state.get("user_id"),
        )
    except Exception:
        out = {"destination": None, "trip_length_days": None, "travelers": [],
               "budget": None, "dietary": [], "other": [],
               "needs_more": True, "missing_required": ["destination"]}

    contract = {
        "destination": out.get("destination"),
        "trip_length_days": out.get("trip_length_days"),
        "travelers": out.get("travelers") or [],
        "budget": out.get("budget"),
        "dietary": out.get("dietary") or [],
        "other": out.get("other") or [],
    }
    detected = _detected_labels(contract)
    suggested = [c for c in _SUPPORTED if c not in detected]
    missing_required = out.get("missing_required") or []
    needs_more = bool(out.get("needs_more"))

    log_step(state.get("thread_id", "-"), "02_extract_requirements",
             {"node": "extract_requirements", "detected": detected,
              "missing_required": missing_required, "needs_more": needs_more})

    return {**state,
            "constraints": contract,
            "detected_constraints": detected,
            "suggested_constraints": suggested,
            "needs_clarification": needs_more,
            "clarification_question": (
                f"Could you tell me your {' and '.join(missing_required)}?"
                if needs_more else None),
            }
