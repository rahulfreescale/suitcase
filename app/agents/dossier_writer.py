"""Dossier writer — composes the audited specialist sections into the final
Style-C dossier, in the premium concierge voice.

The writer's job is voice and cohesion, not new facts. It receives the itinerary
(the structured spine) plus the approved prose sections, and produces:
  - a title + standfirst in concierge voice
  - an ordered set of rendered sections
It returns STRUCTURED data (not HTML) so the UI owns presentation - the itinerary
and map render from the same planState the card view uses.
"""
from __future__ import annotations
from app import gateway
from app.agents.dossier_specialists import _constraint_line


_TITLE_SYS = (
    "You are the lead writer for a premium travel concierge with a warm, personal "
    "'your trip designer' voice. Given a destination, trip length, and the "
    "traveler's constraints, write a dossier TITLE and a one-sentence standfirst.\n"
    "Return STRICT JSON: {\"title\":\"...\", \"standfirst\":\"...\"}.\n"
    "The title is evocative but clear (e.g. 'Rome in Three Days', 'Kyoto, Unhurried'). "
    "The standfirst is one warm sentence that signals the trip is shaped around the "
    "traveler's needs — accessibility as grace, not disclaimer. No clichés like "
    "'nestled' or 'hidden gem'."
)


def _title_and_standfirst(contract, user_id=None) -> dict:
    dest = contract.get("destination") or "your destination"
    days = contract.get("trip_length_days")
    length = f"{days} days" if days else "a few days"
    constraints = _constraint_line(contract)
    try:
        out = gateway.chat_json(
            "writer",
            [{"role": "system", "content": _TITLE_SYS},
             {"role": "user", "content":
              f"Destination: {dest}. Length: {length}. Traveler: {constraints}."}],
            user_id=user_id,
        )
        title = (out.get("title") or "").strip()
        standfirst = (out.get("standfirst") or "").strip()
    except Exception:
        title, standfirst = "", ""
    if not title:
        title = f"{dest} in {length}"
    if not standfirst:
        standfirst = (f"A trip through {dest}, shaped around how you actually move — "
                      f"with the honest notes most planners leave out.")
    return {"title": title, "standfirst": standfirst}


# section display order in the Style-C dossier
_ORDER = ["sense_of_place", "itinerary", "trip_intel", "logistics",
          "weather", "practical", "access_services"]


def _chips_from_contract(contract: dict) -> dict:
    """Derive plan-view constraint chips from the contract (for when the plan is
    reconstructed from a dossier build). Mirrors the shape plan_pipeline emits."""
    detected = []
    trav = contract.get("travelers") or []
    if any((t or {}).get("mobility") == "wheelchair" for t in trav):
        detected.append("wheelchair")
    if any((t or {}).get("type") == "toddler" for t in trav):
        detected.append("toddler")
    if any((t or {}).get("type") == "senior" for t in trav):
        detected.append("senior")
    for d in (contract.get("dietary") or []):
        detected.append(d)
    if contract.get("budget"):
        detected.append("budget")
    return {"detected": detected, "suggested": []}


def compose_dossier(contract: dict, itinerary: dict, sections: list[dict],
                    audit: dict, access_block: dict, user_id=None) -> dict:
    """Assemble the final dossier object the UI renders."""
    ts = _title_and_standfirst(contract, user_id=user_id)

    by_name = {s["section"]: s for s in sections}
    if access_block:
        by_name[access_block["section"]] = access_block

    ordered = []
    for name in _ORDER:
        s = by_name.get(name)
        if not s:
            continue
        if name == "itinerary":
            ordered.append({"section": "itinerary", "title": s.get("title", "The Itinerary"),
                            "itinerary": s.get("itinerary", itinerary)})
        elif name == "access_services":
            ordered.append(s)  # reference block, rendered as a list
        elif s.get("body"):
            ordered.append({"section": name, "title": s.get("title", ""),
                            "body": s["body"]})

    dest = contract.get("destination")
    days = contract.get("trip_length_days")
    meta = {
        "destination": dest,
        "days": days,
        "constraints": _constraint_line(contract),
    }

    return {
        "kind": "dossier",
        "title": ts["title"],
        "standfirst": ts["standfirst"],
        "meta": meta,
        "sections": ordered,
        "itinerary": itinerary,       # for the map + structured render
        "_travelers": contract.get("travelers") or [],   # for plan-view reconstruction
        "_chips": _chips_from_contract(contract),         # constraint chips for plan view
        "audit": {                    # surfaced so the UI can show the quality gate ran
            "quality_score": audit.get("quality_score"),
            "accessibility_ok": audit.get("accessibility_ok"),
            "coherence_notes": audit.get("coherence_notes"),
            "rounds": audit.get("rounds", 1),
        },
    }
