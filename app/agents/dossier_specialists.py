"""Dossier specialists — a team of agents, each drafting ONE section of a
professional trip dossier, every one accessibility-aware.

Design:
  - The itinerary specialist wraps the existing constraint-faithful pipeline
    (plan_trip). It is the ONLY specialist that places activities, and it does so
    through the deterministic hard-constraint rater. It is the spine; every other
    specialist may only REFERENCE places the itinerary already placed.
  - The other five specialists draft prose sections grounded in the guide corpus
    and the itinerary's placed/left-out places. They run in parallel.
  - Each returns a uniform section dict: {section, title, body, refs, flags}.
    `flags` lets a specialist self-report an accessibility concern the auditor
    should scrutinise.

Accessibility is the through-line: every prose specialist is instructed to keep
the traveler's constraints front of mind and never to suggest anything that
undercuts the itinerary's accessibility decisions. The auditor enforces this.
"""
from __future__ import annotations
from pathlib import Path
from app import gateway
from app.agents.plan_pipeline import plan_trip
from app.config import get_settings

settings = get_settings()
from app.tools import travel_data


_GUIDE_DIR = Path("data/travel_guides")


def _guide_section(city: str, header: str) -> str:
    """Return the text under a '## {header}' section of a city's guide, or ''.

    Grounds a specialist in the ACTUAL corpus text instead of model priors -
    the same principle as the itinerary side: describe from data, don't invent.
    """
    if not city:
        return ""
    path = _GUIDE_DIR / f"{city.replace(' ', '_')}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    out, capturing = [], False
    for line in text.splitlines():
        if line.strip().startswith("## "):
            capturing = header.lower() in line.lower()
            continue
        if capturing:
            out.append(line)
    return "\n".join(out).strip()


def _month_to_start_date(month: str | None) -> str | None:
    """Turn a month/season name ('August', 'summer') into a representative ISO
    date (the 15th of that month, next occurrence) so the weather tool returns
    month-specific seasonal norms. Returns None if it can't parse one."""
    if not month:
        return None
    from datetime import date
    m = month.strip().lower()
    months = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
              "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
              "november": 11, "december": 12,
              "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
              "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
              # seasons -> a representative month (northern hemisphere)
              "winter": 1, "spring": 4, "summer": 7, "fall": 10, "autumn": 10}
    mo = None
    for key, num in months.items():
        if key in m:
            mo = num
            break
    if mo is None:
        return None
    today = date.today()
    year = today.year if mo >= today.month else today.year + 1
    return date(year, mo, 15).isoformat()


def _placed_coords(itinerary: dict) -> list[dict]:
    """Placed stops as [{name,lat,lng}] in day+slot order, for routing/weather."""
    out = []
    for day in (itinerary.get("days") or []):
        for slot in ("morning", "afternoon", "evening"):
            blk = (day.get("blocks") or {}).get(slot)
            if blk and blk.get("name_hint"):
                d = {"name": blk["name_hint"]}
                if blk.get("lat") is not None and blk.get("lng") is not None:
                    d["lat"], d["lng"] = blk["lat"], blk["lng"]
                out.append(d)
    return out


def _wheelchair_trip(contract: dict) -> bool:
    return any((t or {}).get("mobility") == "wheelchair"
               for t in (contract.get("travelers") or []))


def _primary_access_constraint(contract: dict) -> str:
    """Which access filter to query OSM with. Wheelchair takes priority (strictest),
    then stroller/toddler, else wheelchair as a sensible default for the dining tool.
    """
    travelers = contract.get("travelers") or []
    if any((t or {}).get("mobility") == "wheelchair" for t in travelers):
        return "wheelchair"
    if any((t or {}).get("type") == "toddler" for t in travelers):
        # a toddler usually means a stroller AND a need for changing tables;
        # 'stroller' is the better venue-entry filter, changing_table comes as extra
        return "stroller"
    return "wheelchair"


def _has_toddler(contract: dict) -> bool:
    return any((t or {}).get("type") == "toddler"
               for t in (contract.get("travelers") or []))


def _has_senior(contract: dict) -> bool:
    return any((t or {}).get("type") == "senior"
               for t in (contract.get("travelers") or []))


# ---- shared helpers -------------------------------------------------------

def _placed_places(itinerary: dict) -> list[str]:
    out = []
    for day in (itinerary.get("days") or []):
        for slot, blk in (day.get("blocks") or {}).items():
            if blk and blk.get("name_hint"):
                out.append(blk["name_hint"])
    return out


def _left_out(itinerary: dict) -> list[str]:
    return [s.get("name_hint") for s in (itinerary.get("skipped") or []) if s.get("name_hint")]


def _constraint_line(contract: dict) -> str:
    """A compact, human phrasing of the traveler's constraints for prompts."""
    bits = []
    trav = contract.get("travelers") or []
    if any((t or {}).get("mobility") == "wheelchair" for t in trav):
        bits.append("a wheelchair user (step-free access is essential)")
    if any((t or {}).get("type") == "toddler" for t in trav):
        bits.append("traveling with a toddler / stroller")
    if any((t or {}).get("type") == "senior" for t in trav):
        bits.append("traveling with a senior (limited walking, needs rest stops)")
    for d in (contract.get("dietary") or []):
        bits.append(f"dietary: {d}")
    if contract.get("budget"):
        bits.append(f"budget-conscious ({contract['budget']})")
    return "; ".join(bits) or "no special constraints stated"


_ACCESS_GUARDRAIL = (
    "ACCESSIBILITY IS THE PRIORITY. The traveler is: {constraints}. "
    "Never recommend or route through anything that would not work for them "
    "(stairs-only entrances, cobbled steep lanes, inaccessible transit). If you "
    "mention a place, it must be one already in the itinerary, or clearly "
    "accessible. If unsure, say so plainly rather than overpromising. "
    "\n\nOUTPUT FORMAT (REQUIRED, overrides any style note above): "
    "Do NOT write a markdown header or title (no lines starting with '#'). "
    "Do NOT write a single long paragraph. You MUST structure the response as "
    "one short lead sentence, THEN 2-5 bullet points (each line starting with '- '). "
    "Put the most important words, place names and numbers in **bold**. Keep it "
    "tight and skimmable, not flowery. This formatting is mandatory."
)


def _draft(task_role: str, system: str, user: str, user_id=None) -> str:
    try:
        return gateway.chat(
            task_role,
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            user_id=user_id,
        ).strip()
    except Exception as e:
        return f"(section unavailable: {e})"


# ---- 1. itinerary specialist (wraps the constraint-faithful pipeline) ------

def spec_itinerary(request: str, user_id=None, use_cache=True, progress=None) -> dict:
    """The spine. Runs the real pipeline; returns its itinerary verbatim.

    This is NOT prose - it's the structured, constraint-checked plan the whole
    dossier is built around. Everything else references it.
    """
    plan = plan_trip(request, user_id=user_id, use_cache=use_cache, progress=progress)
    return {
        "section": "itinerary",
        "title": "The Itinerary",
        "itinerary": plan.get("itinerary") or {},
        "contract": plan.get("contract") or {},
        "chips": plan.get("chips") or {},
        "needs_clarification": plan.get("needs_clarification"),
        "clarification_question": plan.get("clarification_question"),
        "empty_reason": plan.get("empty_reason"),
    }


# ---- 2..6 prose specialists ------------------------------------------------

def spec_logistics(contract, itinerary, user_id=None) -> dict:
    c = _constraint_line(contract)
    dest = contract.get("destination") or "the city"
    wheelchair = _wheelchair_trip(contract)

    # --- REAL DATA: route the legs of each day between geocoded stops ---
    route_facts = []
    if settings.enable_routing_tool and (settings.ors_api_key):
        for day in (itinerary.get("days") or []):
            stops = []
            for slot in ("morning", "afternoon", "evening"):
                blk = (day.get("blocks") or {}).get(slot)
                if blk and blk.get("lat") is not None:
                    stops.append({"name": blk["name_hint"], "lat": blk["lat"], "lng": blk["lng"]})
            if len(stops) >= 2:
                rd = travel_data.route_day(stops, wheelchair=wheelchair)
                for leg in rd.get("legs", []):
                    if leg.get("summary"):
                        route_facts.append(f"{leg['from']} \u2192 {leg['to']}: {leg['summary']}")
    # --- CORPUS GROUNDING: the guide's own transit/access notes ---
    getting_around = _guide_section(dest, "Getting Around")

    grounding = ""
    if route_facts:
        grounding += ("REAL ROUTED DISTANCES (accessible profile; use these, don't "
                      "invent distances):\n- " + "\n- ".join(route_facts[:8]) + "\n")
    if getting_around:
        grounding += f"GUIDE TRANSIT NOTES:\n{getting_around[:600]}\n"

    placed = ", ".join(_placed_places(itinerary)) or "the planned stops"
    sys = ("You are the getting-around specialist for a premium, accessibility-first "
           "travel concierge. Using the grounding provided (real routed distances and "
           "the guide's transit notes), write a warm, concrete 'Getting Around' "
           "section (90-130 words). Favor step-free transit, and where a leg is "
           "flagged too far to wheel, say so and suggest an accessible taxi. Use the "
           "REAL distances if given; don't invent numbers. "
           + _ACCESS_GUARDRAIL.format(constraints=c))
    usr = (f"Destination: {dest}. The itinerary visits: {placed}.\n\n{grounding}\n"
           f"Write the Getting Around section grounded in the above.")
    return {"section": "logistics", "title": "Getting Around",
            "body": _draft("clarify", sys, usr, user_id), "refs": [], "flags": [],
            "tool_data": route_facts}


def spec_dining(contract, itinerary, user_id=None) -> dict:
    c = _constraint_line(contract)
    dest = contract.get("destination") or "the city"
    diet = ", ".join(contract.get("dietary") or []) or "no dietary restriction"
    wheelchair = _wheelchair_trip(contract)

    # --- REAL DATA: pull eateries matching the traveler's ACCESS constraint ---
    constraint = _primary_access_constraint(contract)
    toddler = _has_toddler(contract)
    real_places = []
    coords = [s for s in _placed_coords(itinerary) if s.get("lat") is not None]
    if coords and settings.enable_places_tool:
        center = coords[len(coords) // 2]
        res = travel_data.accessible_places(center["lat"], center["lng"],
                                            kind="food", constraint=constraint)
        for p in res.get("places", [])[:6]:
            bits = [p["name"]]
            if p.get("cuisine"): bits.append(p["cuisine"].replace(";", ", "))
            bits.append(f"{constraint}: {p.get('access')}")
            if p.get("toilets_wheelchair"): bits.append(f"accessible WC: {p['toilets_wheelchair']}")
            if p.get("changing_table"): bits.append("baby changing: yes")
            if p.get("highchair"): bits.append("highchairs: yes")
            real_places.append(" — ".join(bits))
        places_note = res.get("note", "")
    else:
        places_note = ""

    hoods = _guide_section(dest, "Neighborhoods")
    grounding = ""
    if real_places:
        grounding += ("REAL accessible eateries near the route (OpenStreetMap; "
                      "prefer these actual places over invented ones):\n- "
                      + "\n- ".join(real_places) + "\n")
    if places_note:
        grounding += f"COVERAGE NOTE: {places_note}\n"
    if hoods:
        grounding += f"GUIDE NEIGHBORHOODS (for area character):\n{hoods[:400]}\n"

    sys = ("You are the dining specialist for a premium, accessibility-first travel "
           "concierge. Recommend 2-3 dining ideas that fit the traveler. If REAL "
           "accessible eateries are provided below, feature those by name and note "
           "their access; do NOT invent named restaurants beyond them. If none are "
           "provided, describe the KIND of place and real neighborhood instead, and "
           "advise calling ahead to confirm access. Always honor dietary needs and "
           "note step-free entrance / table access. Warm, appetizing, 100-150 words. "
           + _ACCESS_GUARDRAIL.format(constraints=c))
    usr = (f"Destination: {dest}. Dietary: {diet}. Wheelchair traveler: {wheelchair}.\n\n"
           f"{grounding}\nWrite the dining section, grounded in the real places above "
           f"where given.")
    return {"section": "dining", "title": "The Table",
            "body": _draft("clarify", sys, usr, user_id), "refs": [], "flags": [],
            "tool_data": real_places}


def spec_weather(contract, itinerary, user_id=None) -> dict:
    c = _constraint_line(contract)
    dest = contract.get("destination") or "the city"
    coords = _placed_coords(itinerary)
    located = [s for s in coords if s.get("lat") is not None]

    # --- REAL DATA: pull weather (+ air quality) at the first located stop ---
    # Resolve the travel month the user gave (contract.preferences.travel_month,
    # e.g. "August") into a representative date so the weather tool returns
    # month-specific norms instead of a generic full-year range.
    start_date = contract.get("start_date") or _month_to_start_date(
        (contract.get("preferences") or {}).get("travel_month"))
    tool_facts = []
    if located and settings.enable_weather_tool:
        w = travel_data.weather(located[0]["lat"], located[0]["lng"],
                                start=start_date)
        if w.get("summary"):
            tool_facts.append(f"Weather ({w.get('source')}): {w['summary']}")
    if located and settings.enable_airquality_tool:
        aq = travel_data.air_quality(located[0]["lat"], located[0]["lng"])
        if aq.get("summary"):
            tool_facts.append(f"Air quality: {aq['summary']}")
    # --- CORPUS GROUNDING: the guide's own seasonal note ---
    season_note = _guide_section(dest, "Best Time to Visit")

    grounding = ""
    if tool_facts:
        grounding += "REAL DATA (use these figures, don't invent others):\n- " + "\n- ".join(tool_facts) + "\n"
    if season_note:
        grounding += f"GUIDE SEASONAL NOTE:\n{season_note[:500]}\n"

    placed = ", ".join(s["name"] for s in coords) or "the planned stops"
    sys = ("You are the weather specialist for a travel concierge. You write a short "
           "'What to Expect' note from the data you're given. "
           "CRITICAL RULES:\n"
           "1. You have ALL the data you need below. NEVER ask the reader for a month, "
           "dates, or any information. NEVER say 'I'd be happy to' or 'once you share'. "
           "Just write the note from the figures provided.\n"
           "2. If the data is a full-year range, describe that range (e.g. coldest vs "
           "warmest months) as the answer — do not treat missing dates as a blocker.\n"
           "3. Do NOT connect weather to wheelchair access unless weather genuinely "
           "affects mobility (ice, extreme heat, rain on cobbles). Usually it doesn't — "
           "so usually don't mention accessibility at all here.\n"
           "4. FORMAT: one short opening sentence, then 2-4 bullet points (each line "
           "starting with '- '), with the **temperatures and key numbers in bold**. "
           "No markdown '#' headers. 60-100 words total.\n"
           "Write ONLY the note itself — no preamble, no questions, no offers.")
    usr = (f"Destination: {dest}.\n\n{grounding}\n"
           f"Write the 'What to Expect' note now from the data above. Output the note "
           f"directly — bullets with bold numbers, no questions.")
    return {"section": "weather", "title": "What to Expect",
            "body": _draft("clarify", sys, usr, user_id), "refs": [], "flags": [],
            "tool_data": tool_facts}


def spec_sense_of_place(contract, itinerary, user_id=None) -> dict:
    c = _constraint_line(contract)
    dest = contract.get("destination") or "the city"
    sys = ("You are the narrative specialist for a premium travel concierge — you "
           "write the evocative connective tissue that makes a dossier feel literary. "
           "Write a short 'sense of the place' opening (90-130 words): what this city "
           "FEELS like, the mood of the days ahead, warm and vivid but never purple. "
           "Second person, present tense, concierge warmth. Touch lightly on how the "
           "trip has been shaped for ease of movement, as a grace note not a "
           "disclaimer. " + _ACCESS_GUARDRAIL.format(constraints=c))
    usr = f"Destination: {dest}. Write the opening sense-of-place passage."
    return {"section": "sense_of_place", "title": "The City, Briefly",
            "body": _draft("clarify", sys, usr, user_id), "refs": [], "flags": []}


def spec_practical(contract, itinerary, user_id=None) -> dict:
    c = _constraint_line(contract)
    dest = contract.get("destination") or "the city"
    placed = ", ".join(_placed_places(itinerary)) or "the planned stops"
    budget_note = _guide_section(dest, "Budget Notes")
    ground = (f"GUIDE BUDGET NOTES (ground the budget line in this):\n{budget_note[:400]}\n"
              if budget_note else "")

    # --- constraint-specific REAL DATA for families / seniors ---
    coords = [s for s in _placed_coords(itinerary) if s.get("lat") is not None]
    if coords and settings.enable_places_tool:
        center = coords[len(coords) // 2]
        if _has_toddler(contract):
            ta = travel_data.toddler_activities(center["lat"], center["lng"])
            names = [p["name"] for p in ta.get("places", [])[:4]]
            if names:
                ground += ("NEARBY TODDLER OPTIONS (real, OSM - mention 1-2, esp. indoor "
                           "for rainy days): " + "; ".join(names) + "\n")
        if _has_senior(contract):
            rs = travel_data.rest_stops(center["lat"], center["lng"])
            if rs.get("note") and rs.get("kind") == "rest_stops":
                ground += f"REST-STOP DENSITY (real, OSM): {rs['note']}\n"

    # --- CLOSURES: public holidays during the trip (trip-ruining if missed) ---
    if settings.enable_holidays_tool:
        hol = travel_data.holidays_in_window(dest, contract.get("start_date"),
                                             contract.get("end_date"))
        hits = hol.get("holidays") or []
        if hits:
            listing = "; ".join(f"{h['date']} {h['name']}" for h in hits[:4])
            ground += ("PUBLIC HOLIDAY DURING TRIP (IMPORTANT - warn the traveler "
                       "clearly; many attractions close and transit is reduced): "
                       + listing + "\n")
    sys = ("You are the practical-prep specialist for a premium, accessibility-first "
           "travel concierge. Write a concise 'Before You Go' section (90-130 words): "
           "a rough budget sensibility (grounded in the guide notes if given), and - "
           "most valuable - an accessibility prep note (confirm lifts/step-free routes "
           "are running before you go, carry disability documentation for "
           "companion/priority entry, arrange equipment rental ahead). For booking, "
           "speak GENERALLY - e.g. 'popular timed-entry sites are worth reserving' and "
           "'confirm access by phone' - do NOT claim specific availability, prices, or "
           "that a named place needs booking, since you don't have live booking data. "
           "If nearby toddler options or rest-stop density are provided, weave them in "
           "naturally (e.g. an indoor play centre as a rainy-day backup, or reassurance "
           "about benches for rest). If a PUBLIC HOLIDAY falls during the trip, open "
           "with a clear, friendly heads-up naming the date and holiday, and advise "
           "checking attraction hours and expecting reduced transit that day. "
           "Practical, reassuring, honest about what to verify. "
           + _ACCESS_GUARDRAIL.format(constraints=c))
    usr = (f"Destination: {dest}. The plan includes: {placed}.\n\n{ground}\n"
           f"Write the Before You Go section with an accessibility prep note.")
    return {"section": "practical", "title": "Before You Go",
            "body": _draft("clarify", sys, usr, user_id), "refs": [], "flags": []}


# ---- accessibility-services reference block (DATA field, not an agent) ------

def accessibility_services(contract) -> dict:
    """A reference block, not a reasoning agent. Placeholder/corpus-based for now;
    a real deployment would source these from a local accessibility directory.
    The emergency number is country-aware so it isn't wrong for the destination."""
    dest = contract.get("destination") or "your destination"
    # country-aware emergency number (was hardcoded "112 (EU)", wrong outside the EU).
    # _country_for returns ISO codes (IN, IT, US...), so key on those.
    _EMERGENCY = {
        "IN": "112 (national) / 100 police, 102 ambulance",
        "US": "911", "CA": "911", "GB": "999 / 112",
        "JP": "110 police / 119 ambulance", "AU": "000",
        "CN": "110 police / 120 ambulance", "MX": "911",
        "BR": "190 police / 192 ambulance", "AE": "999 / 112",
        "TH": "191 police / 1669 ambulance", "TR": "112",
        "EG": "122 police / 123 ambulance", "ZA": "10111 / 112",
        "SG": "999 police / 995 ambulance", "NZ": "111",
        "KR": "112 police / 119 ambulance", "MA": "19 police / 15 ambulance",
        "KE": "999 / 112", "AR": "911", "VN": "113 police / 115 ambulance",
        "JO": "911", "PE": "105 police / 106 ambulance", "IS": "112",
        "HK": "999", "TW": "110 police / 119 ambulance",
    }
    _EU_CODES = {"IT", "FR", "ES", "DE", "NL", "PT", "GR", "AT", "BE", "IE",
                 "CZ", "PL", "HU", "HR", "SE", "DK", "FI"}
    try:
        from app.tools.travel_data import _country_for
        country = _country_for(dest)
    except Exception:
        country = None
    if country and country in _EMERGENCY:
        emergency = _EMERGENCY[country]
    elif country and country in _EU_CODES:
        emergency = "112 (EU-wide emergency number)"
    else:
        emergency = "check your destination's local emergency number"
    return {
        "section": "access_services",
        "title": "Accessibility Services",
        "is_reference": True,
        "note": ("Reference information — confirm details locally before you rely on "
                 "them; availability changes."),
        "items": [
            {"label": "Emergency", "value": emergency},
            {"label": "Accessible transport", "value": f"Pre-book accessible taxis in {dest} a day ahead where possible"},
            {"label": "Equipment rental", "value": "Wheelchair / mobility-scooter rental is available in most major cities — arrange before arrival"},
            {"label": "Before you visit", "value": "Call major sites 24-48h ahead to confirm lifts and step-free routes are in service"},
        ],
    }


# ---- the parallel-runnable set --------------------------------------------

PROSE_SPECIALISTS = [spec_sense_of_place, spec_logistics,
                     spec_weather, spec_practical]
