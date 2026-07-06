"""True tool-calling agents for Suitcase.

Unlike the workflow steps (extraction, prose specialists, writer) — which are
single-shot LLM calls orchestrated by code — the agents here own their own
control flow. Each is given a set of tool schemas and a tool-calling loop
(gateway.chat_tools); the MODEL decides which tools to call, with what arguments,
how many times, and when it has enough to answer. That model-owned control flow
is what makes these agents rather than workflow nodes.

Three agents:
  - research_agent   : gathers trip-specific enrichment; which tools matter
                       depends on the trip (wheelchair city vs toddler beach).
  - rating_agent     : for uncertain / out-of-corpus places, investigates real
                       accessibility evidence (OSM tags, routes) before rating.
  - verifier_agent   : fact-checks a drafted brief against tools, deciding which
                       claims to verify based on what the draft actually says.

Everything stays grounded: the agents can only report what the tools returned.
"""
from __future__ import annotations
import json
from app import gateway
from app.tools import travel_data
from app.config import get_settings
from app.observability import observe

_s = get_settings()


# --------------------------------------------------------------------------
# Tool schemas (OpenAI-style) + the registry that maps names -> callables.
# These are the "menu" the agents choose from.
# --------------------------------------------------------------------------
def _tool(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


_LATLNG = {"lat": {"type": "number", "description": "latitude"},
           "lng": {"type": "number", "description": "longitude"}}

TOOL_SCHEMAS = [
    _tool("weather",
          "Seasonal or live weather for a location. Use when the brief needs "
          "what-to-pack / comfort guidance. Returns temps and conditions.",
          _LATLNG, ["lat", "lng"]),
    _tool("air_quality",
          "Current air quality (PM2.5, EAQI) at a location. Use for outdoor "
          "comfort, especially for seniors, toddlers, or respiratory concerns.",
          _LATLNG, ["lat", "lng"]),
    _tool("accessible_places",
          "Find nearby places (food/attractions) filtered by real OSM "
          "accessibility tags (wheelchair, stroller, changing_table). Use to "
          "check what's actually accessible near a point, or to verify a place.",
          {**_LATLNG,
           "constraint": {"type": "string", "enum": ["wheelchair", "stroller", "toddler"],
                          "description": "which accessibility filter to apply"},
           "kind": {"type": "string", "enum": ["food", "attraction"],
                    "description": "type of place"}},
          ["lat", "lng", "constraint"]),
    _tool("route_leg",
          "Distance and step-free/wheelchair route between two points. Use to "
          "check whether two stops are reachable without stairs, and how far.",
          {"from_lat": {"type": "number"}, "from_lng": {"type": "number"},
           "to_lat": {"type": "number"}, "to_lng": {"type": "number"},
           "wheelchair": {"type": "boolean",
                          "description": "true for a wheelchair-safe route profile"}},
          ["from_lat", "from_lng", "to_lat", "to_lng"]),
    _tool("toddler_activities",
          "Find playgrounds and indoor play areas near a point. Use for trips "
          "with a toddler to enrich the day with child-friendly stops.",
          _LATLNG, ["lat", "lng"]),
    _tool("rest_stops",
          "Find benches, water fountains, and public toilets near a point. Use "
          "for wheelchair users and seniors who need rest/facility planning.",
          _LATLNG, ["lat", "lng"]),
    _tool("holidays_in_window",
          "Public holidays (possible closures) for a city between two dates. "
          "Use when the trip has dates, to warn about closures.",
          {"city": {"type": "string"},
           "start": {"type": "string", "description": "YYYY-MM-DD or null"},
           "end": {"type": "string", "description": "YYYY-MM-DD or null"}},
          ["city"]),
    _tool("special_needs_nearby",
          "Find real OSM amenities for a SPECIAL traveler need near a point. "
          "Use when travelers have needs beyond wheelchair/toddler. Categories: "
          "'medical' (pharmacy/hospital - chronic conditions, meds), 'quiet' "
          "(parks/calm places - autism, sensory sensitivity, anxiety), "
          "'allergen_dining' (gluten-free/vegan/vegetarian eateries), 'prayer' "
          "(places of worship, halal/kosher), 'family' (changing tables/nursing), "
          "'parking' (blue-badge accessible parking), 'step_free_transit' "
          "(wheelchair-accessible stations/stops). Returns counts + examples, or "
          "an honest 'sparse' if OSM has nothing mapped.",
          {**_LATLNG,
           "category": {"type": "string",
                        "enum": ["medical", "quiet", "allergen_dining", "prayer",
                                 "family", "parking", "step_free_transit"],
                        "description": "which special-need amenity to look for"}},
          ["lat", "lng", "category"]),

]

# name -> real callable. The loop invokes these when the model asks.
TOOL_REGISTRY = {
    "weather": lambda **k: travel_data.weather(**k),
    "air_quality": lambda **k: travel_data.air_quality(**k),
    "accessible_places": lambda **k: travel_data.accessible_places(**k),
    "route_leg": lambda **k: travel_data.route_leg(**k),
    "toddler_activities": lambda **k: travel_data.toddler_activities(**k),
    "rest_stops": lambda **k: travel_data.rest_stops(**k),
    "holidays_in_window": lambda **k: travel_data.holidays_in_window(**k),
    "special_needs_nearby": lambda **k: travel_data.nearby_amenities(**k),
}


def _stops_digest(itinerary: dict, limit: int = 8) -> list[dict]:
    """Compact [{name,lat,lng,day}] of placed stops, for agent context."""
    out = []
    for d in (itinerary.get("days") or []):
        for slot in ("morning", "afternoon", "evening"):
            b = (d.get("blocks") or {}).get(slot)
            if b and b.get("lat") is not None:
                out.append({"name": b.get("name_hint"), "lat": b["lat"],
                            "lng": b["lng"], "day": d.get("day")})
    return out[:limit]


def _constraint_summary(contract: dict) -> str:
    trav = contract.get("travelers") or []
    bits = []
    if any((t or {}).get("mobility") == "wheelchair" for t in trav):
        bits.append("wheelchair user (step-free essential)")
    if any((t or {}).get("type") == "toddler" for t in trav):
        bits.append("toddler (stroller, changing tables, nap pacing)")
    if any((t or {}).get("type") == "senior" for t in trav):
        bits.append("senior (rest stops, gentle pace)")
    for d in (contract.get("dietary") or []):
        need = d.get("need") if isinstance(d, dict) else d
        bits.append(f"dietary: {need}")
    if contract.get("budget"):
        bits.append("budget-conscious")
    return "; ".join(bits) or "no special constraints"


def _preference_signals(contract: dict) -> str:
    """Turn soft preferences into an explicit instruction the agent can act on —
    e.g. a stated month or a dislike of cold makes weather decision-relevant."""
    p = contract.get("preferences") or {}
    lines = []
    if p.get("travel_month"):
        lines.append(f"- The user is traveling in **{p['travel_month']}** — call "
                     f"`weather` and tailor packing/indoor-vs-outdoor advice to that "
                     f"season. Pass the month if the tool accepts a date.")
    if p.get("climate"):
        lines.append(f"- The user said: \"{p['climate']}\" — this is weather-relevant. "
                     f"Call `weather`, and if the conditions clash with their "
                     f"preference (e.g. they dislike cold and it's cold), say so and "
                     f"steer toward indoor stops or a better-suited time of day.")
    if p.get("interests"):
        lines.append(f"- Interests: {', '.join(p['interests'])} — weight your "
                     f"suggestions toward these where the tools support it.")

    # special needs -> explicit tool instructions (this is what makes these
    # needs actually flow through: extraction sets the flag, we turn it into a
    # concrete 'call this tool' instruction the agent acts on).
    sn = contract.get("special_needs") or {}
    if sn.get("medical"):
        lines.append("- The traveler has a MEDICAL need — call "
                     "`special_needs_nearby` with category 'medical' near the "
                     "planned stops to surface pharmacies and hospitals, and note "
                     "the nearest ones in your write-up.")
    if sn.get("sensory"):
        lines.append("- The traveler has a SENSORY/anxiety need — call "
                     "`special_needs_nearby` with category 'quiet' to find calm "
                     "green/quiet spaces near the stops, and flag which planned "
                     "spots tend to be crowded so they can time visits or take "
                     "breaks.")
    if sn.get("heat_sensitive"):
        lines.append("- The traveler is HEAT/STAMINA-sensitive — call `weather` "
                     "and `rest_stops`; advise on shade, indoor options during peak "
                     "heat, and where to rest. Steer toward early-morning or "
                     "evening for exposed outdoor stops.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# AGENT 1: Research Agent
# --------------------------------------------------------------------------
@observe(name="research_agent")
def research_agent(contract: dict, itinerary: dict, user_id=None, on_step=None) -> dict:
    """TRUE AGENT. Given a trip, decides which enrichment tools are worth calling
    and gathers trip-specific intelligence. The tool choices vary by trip — a
    wheelchair city trip pulls routes + rest stops; a toddler trip pulls
    playgrounds + holidays; a hot-climate trip checks weather + air quality.

    Returns {"section": "trip_intel", "title": ..., "body": <prose>, "trace": [...]}.
    The trace is the record of which tools the model chose to call.
    """
    dest = contract.get("destination") or "the destination"
    stops = _stops_digest(itinerary)
    constraints = _constraint_summary(contract)
    pref_signals = _preference_signals(contract)
    dates = ""
    if contract.get("start_date"):
        dates = f"Dates: {contract.get('start_date')} to {contract.get('end_date')}. "

    sys = (
        "You are the trip-research agent for an accessibility-first travel "
        "concierge. You have tools for weather, air quality, accessible places, "
        "routing, toddler activities, rest stops, and public holidays.\n\n"
        "YOUR JOB: gather the enrichment that THIS trip actually needs, then "
        "write a short 'Good to Know' section from ONLY what the tools return.\n\n"
        "DECIDE which tools matter for these travelers — don't call everything:\n"
        "- wheelchair user -> route_leg between distant stops, rest_stops, and "
        "special_needs_nearby(step_free_transit / parking)\n"
        "- toddler -> toddler_activities, holidays, special_needs_nearby(family)\n"
        "- senior -> rest_stops, weather (heat fatigue), air_quality, "
        "special_needs_nearby(medical)\n"
        "- sensory / autism / anxiety -> special_needs_nearby(quiet)\n"
        "- dietary (allergies, vegan, halal/kosher) -> "
        "special_needs_nearby(allergen_dining / prayer)\n"
        "- medical needs / chronic condition -> special_needs_nearby(medical)\n"
        "- hot/cold climate or packing questions -> weather\n"
        "Call tools with the real coordinates provided. Call only what adds value; "
        "2-5 tool calls is typical. Then STOP and write.\n\n"
        "OUTPUT: one short lead sentence, then 3-5 bullet points (each '- '), "
        "with key numbers/places in **bold**. Ground every claim in tool results. "
        "If a tool returns nothing useful, say so honestly or omit it. No markdown "
        "headers. No fabrication.")
    usr = (f"Destination: {dest}. {dates}Travelers: {constraints}.\n"
           + (f"\nUSER PREFERENCES TO ACT ON:\n{pref_signals}\n" if pref_signals else "")
           + f"\nPlaced stops (name, lat, lng, day):\n{json.dumps(stops, indent=1)}\n\n"
           f"Research what this trip needs, then write the 'Good to Know' section.")

    try:
        result = gateway.chat_tools(
            "dossier", [{"role": "system", "content": sys},
                        {"role": "user", "content": usr}],
            tools=TOOL_SCHEMAS, tool_registry=TOOL_REGISTRY,
            user_id=user_id, max_iters=5, on_step=on_step)
    except Exception as e:
        return {"section": "trip_intel", "title": "Good to Know",
                "body": "", "trace": [], "error": f"{type(e).__name__}: {e}"}

    return {"section": "trip_intel", "title": "Good to Know",
            "body": (result.get("content") or "").strip(),
            "trace": result.get("trace", []),
            "iters": result.get("iters", 0)}


# --------------------------------------------------------------------------
# AGENT 2: Rating / City-Onboarding Agent
# --------------------------------------------------------------------------
# Schema for the agent to RECORD a grounded rating it reached from evidence.
_RATE_TOOL = _tool(
    "record_rating",
    "Record your evidence-based accessibility rating for ONE place, after you've "
    "gathered evidence with the other tools. Call this once per place.",
    {"place": {"type": "string"},
     "lat": {"type": "number"}, "lng": {"type": "number"},
     "wheelchair": {"type": "string", "enum": ["EXCELLENT", "GOOD", "TOUGH", "FAIL", "UNKNOWN"]},
     "toddler": {"type": "string", "enum": ["EXCELLENT", "GOOD", "TOUGH", "FAIL", "UNKNOWN"]},
     "senior": {"type": "string", "enum": ["EXCELLENT", "GOOD", "TOUGH", "FAIL", "UNKNOWN"]},
     "evidence": {"type": "string",
                  "description": "one sentence citing what the tools showed (e.g. "
                  "'OSM tags wheelchair=yes; step-free route confirmed')"},
     "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]}},
    ["place", "wheelchair", "evidence", "confidence"])


@observe(name="onboarding_agent")
def onboarding_agent(city: str, candidate_places: list[dict], user_id=None,
                     on_step=None) -> dict:
    """TRUE AGENT. For an out-of-corpus city, ground each candidate place in REAL
    evidence before rating it — instead of guessing from model memory (which
    produced bad data + bad coordinates, e.g. the Meerut problem).

    The agent DECIDES, per place, how much to investigate: it can call
    accessible_places to read OSM access tags, route_leg to test reachability,
    etc., then records an evidence-based rating with honest confidence. Places it
    can't verify it marks UNKNOWN rather than inventing a rating.

    candidate_places: [{"name","lat","lng"}] the model proposed for this city.
    Returns {"ratings": [ {place,lat,lng,wheelchair,...,evidence,confidence}, ...],
             "trace": [...]}.
    """
    if not candidate_places:
        return {"ratings": [], "trace": []}

    ratings = []
    registry = dict(TOOL_REGISTRY)
    # inject the record_rating sink that captures the agent's verdicts
    registry["record_rating"] = lambda **k: (ratings.append(k) or {"recorded": k.get("place")})
    tools = TOOL_SCHEMAS + [_RATE_TOOL]

    places_json = json.dumps(candidate_places[:8], indent=1)
    sys = (
        "You are the city-onboarding agent for an accessibility-first travel app. "
        "A traveler asked about a city NOT in our vetted guide corpus, so you must "
        "ground each candidate place in REAL evidence before we trust it.\n\n"
        "For each place:\n"
        "1. Use `accessible_places` near its coordinates to check real OSM "
        "accessibility tags (wheelchair, stroller). Use `route_leg` if reachability "
        "matters.\n"
        "2. From what the tools ACTUALLY return, call `record_rating` with an "
        "evidence-based label per constraint and honest confidence.\n"
        "3. If the tools show nothing about a place, rate it UNKNOWN with LOW "
        "confidence — do NOT invent an accessibility rating from memory.\n\n"
        "Be efficient: a couple of tool calls per place is plenty. Record a rating "
        "for every place, then stop.")
    usr = (f"City: {city}. Candidate places (name, lat, lng):\n{places_json}\n\n"
           f"Investigate and record an evidence-based rating for each.")

    try:
        result = gateway.chat_tools(
            "clarify", [{"role": "system", "content": sys},
                        {"role": "user", "content": usr}],
            tools=tools, tool_registry=registry, user_id=user_id,
            max_iters=max(4, len(candidate_places) * 2), on_step=on_step)
    except Exception as e:
        return {"ratings": ratings, "trace": [], "error": f"{type(e).__name__}: {e}"}

    # normalise each recorded verdict: the agent stores its reasoning under
    # "evidence"; downstream (CSV/display) reads "note", so mirror it across.
    for r in ratings:
        if not r.get("note") and r.get("evidence"):
            r["note"] = r["evidence"]

    return {"ratings": ratings,
            "trace": (result or {}).get("trace", []),
            "iters": (result or {}).get("iters", 0)}
