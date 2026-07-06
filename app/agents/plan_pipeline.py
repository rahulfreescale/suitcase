"""Phase 4a - Constraint-planning pipeline (backend chain, no UI).

Runs the NEW hero feature end-to-end against REAL retrieval, so we can verify a
live query produces a correct structured plan before wiring any UI.

Chain:
    request
      -> Phase 1  extract_requirements   -> constraint contract (+ chips)
      -> retrieve REAL guide chunks for the destination (existing RAG stack)
      -> Phase 2  rate each chunk         -> per-constraint fit
      -> Phase 3  assemble                -> rated day plan + skipped + critique

This is intentionally SEPARATE from the existing clarify->plan->research->write
graph. That graph still answers open questions. This pipeline is the dedicated
"plan a constrained trip" path. Phase 4b decides how they surface together in
the app; 4a just proves the chain.

Retrieval note: we reuse the real hybrid+rerank stack via run_rag(), but we want
MULTIPLE distinct activities (not one assembled context blob), so we pull the
ranked citations/chunks and rate each. run_rag already returns per-chunk
citations with city/page/quote - exactly what the rater consumes.
"""
from app import gateway
from app.agents.extract_requirements import extract_requirements
from app.agents.rate_fit import rate_activity
from app.agents.assemble import assemble_itinerary
from app.agents.decompose import decompose_all


def _corpus_cities() -> list[str]:
    """Cities we ship curated guides for — used ONLY for 'did you mean?' hints,
    NOT as an allow-list (new cities still self-warm via the lazy bank)."""
    import os
    from app.config import get_settings
    try:
        return [f[:-3].replace("_", " ")
                for f in os.listdir(get_settings().travel_guides_dir)
                if f.endswith(".md")]
    except Exception:
        return []


def _closest_city(dest: str, cities: list[str]) -> str | None:
    """Fuzzy 'did you mean?' — catches typos like room->Rome. Only used to
    SUGGEST when a plan came back empty; never to reject a destination."""
    import difflib
    if not dest:
        return None
    matches = difflib.get_close_matches(dest.lower(),
                                        [c.lower() for c in cities], n=1, cutoff=0.7)
    if not matches:
        return None
    for c in cities:
        if c.lower() == matches[0]:
            return c
    return None


def _norm_name(s: str) -> str:
    """Loose normalization for deduping place names across bank + retrieval."""
    import re, unicodedata
    s = (s or "").lower()
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    stop = {"the", "a", "an", "of", "and"}
    return " ".join(w for w in s.split() if w not in stop)
from app.tools.rag_tool import run_rag
from app.observability import observe


def _retrieve_activities(contract: dict, extra_queries: list[str] | None = None) -> list[dict]:
    """Pull real ranked guide chunks for the destination, as rateable activities.

    We issue a few targeted retrieval queries to surface a spread of activities
    (things to do + the guide's challenging spots), then dedupe by quote. Each
    returned item is {city, page, text} - the shape rate_activity expects.
    """
    city = contract.get("destination") or ""
    queries = [
        f"things to do in {city}",
        f"{city} family activities parks attractions",
        f"{city} popular but challenging accessibility",
    ]
    if extra_queries:
        queries += extra_queries

    seen, activities = set(), []
    _want = _norm_name(city)
    for q in queries:
        res = run_rag(q)
        for c in res.get("citations", []):
            quote = (c.get("quote") or "").strip()
            key = quote[:80]
            if not quote or key in seen:
                continue
            # CITY GUARD: RAG returns nearest-neighbour chunks regardless of city,
            # so an unknown/fake destination pulls in chunks from OTHER cities.
            # Only keep chunks actually tagged with the requested city; if the
            # chunk has a city and it doesn't match, drop it (don't relabel it).
            chunk_city = _norm_name(c.get("city") or "")
            if chunk_city and _want and chunk_city != _want:
                continue
            seen.add(key)
            activities.append({
                "city": c.get("city") or city,
                "page": c.get("page"),
                "text": quote,
            })
    return activities


def _gather_evidence(activity: dict, contract: dict) -> str:
    """Fetch guide detail about THIS specific activity's accessibility/family fit.

    The activity's own description (from decomposition) is often thin - a name +
    a line. The accessibility detail lives in the guide's Family/Challenging
    sections, keyed by place name. We retrieve for "<place> <city> wheelchair
    accessible family" and fold any sentence that actually mentions this place
    into the text the rater judges. Falls back to the original description if the
    targeted search surfaces nothing about this place.
    """
    name = (activity.get("name") or "").strip()
    city = (activity.get("city") or contract.get("destination") or "").strip()
    base = activity.get("text", "")
    if not name:
        return base

    # build a targeted query using the traveler's actual needs
    needs = []
    trav = contract.get("travelers") or []
    if any((t or {}).get("mobility") == "wheelchair" for t in trav):
        needs.append("wheelchair accessible step-free")
    if any((t or {}).get("type") == "toddler" for t in trav):
        needs.append("family toddler stroller")
    if any((t or {}).get("type") == "senior" for t in trav):
        needs.append("senior mobility")
    query = f"{name} {city} {' '.join(needs) or 'accessibility family'}"

    try:
        res = run_rag(query)
    except Exception:
        return base

    # keep only sentences that actually reference THIS place (avoid pulling in
    # detail about a different landmark).
    name_lc = name.lower()
    key_tokens = [w for w in name_lc.replace("'", " ").split() if len(w) > 3]
    extra = []
    for c in res.get("citations", []):
        quote = (c.get("quote") or "")
        for sent in quote.replace("\n", " ").split("."):
            s = sent.strip()
            if not s:
                continue
            sl = s.lower()
            if name_lc in sl or any(tok in sl for tok in key_tokens):
                extra.append(s)
    extra = list(dict.fromkeys(extra))[:4]  # dedupe, cap
    if not extra:
        return base
    return (base + " " + " ".join(extra)).strip()


def _lazy_build_bank(city: str, user_id: str | None = None) -> None:
    """Build a bank for a city that doesn't have one yet, at request time, and
    cache it to data/banks/<City>_accessibility.csv so future requests are fast.

    This is the read-through / self-warming path: first request for a new city
    pays a one-time build cost; every request after serves from the cached CSV.
    Quality is guide-derived (lower confidence than a hand-researched bank) - the
    confidence column reflects that honestly.
    """
    import csv
    from pathlib import Path
    from app.stores import bank as bank_store

    # discover the city's places from retrieval + decomposition
    contract_stub = {"destination": city,
                     "travelers": [{"type": "adult", "mobility": "wheelchair"},
                                   {"type": "toddler", "mobility": None}]}
    chunks = _retrieve_activities(contract_stub)
    # Only trust retrieval if the chunks are actually ABOUT this city. RAG returns
    # nearest-neighbour chunks regardless of city, so an out-of-corpus request
    # ("Meerut") can surface Delhi chunks tagged as Delhi. Guard against seeding a
    # bank of the wrong city's places under this name.
    _norm_city = (city or "").strip().lower().replace(" ", "_")
    chunks = [c for c in chunks
              if (c.get("city") or "").strip().lower().replace(" ", "_") == _norm_city]
    places = decompose_all(chunks, user_id=user_id)

    rows = []
    if places:
        # ---- Path A: guide-grounded. We have retrieved text for this city;
        # extract per-place accessibility facts from it (higher trust). ----
        rows = _bank_rows_from_guide(city, places, contract_stub, user_id)

    if not rows:
        # ---- Path B: LLM-knowledge fallback. Nothing in the corpus for this
        # city (truly out-of-corpus). Ask the model for the city's top places
        # and their accessibility from its own knowledge, and seed a bank so
        # next time is fast. Flagged source="llm knowledge", confidence=LOW so
        # these unverified ratings SOFTEN (never hard-block) until a real guide
        # or hand-research replaces them. ----
        rows = _bank_rows_from_llm_knowledge(city, user_id)

    if not rows:
        return

    # Backfill coordinates so weather, air-quality and the map work for this
    # lazily-built city (hand-researched banks already carry lat/lng; the lazy
    # path historically didn't, which broke those features for new cities).
    from app.tools import travel_data as _td
    for r in rows:
        if not r.get("lat") or not r.get("lng"):
            coords = _td.geocode_place(r.get("place", ""), city)
            if coords:
                r["lat"], r["lng"] = coords[0], coords[1]

    bank_dir = Path("data/banks")
    bank_dir.mkdir(parents=True, exist_ok=True)
    path = bank_dir / f"{city.replace(' ', '_')}_accessibility.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["city", "place", "is_famous", "wheelchair",
                                          "toddler", "senior", "confidence", "note",
                                          "source", "lat", "lng"])
        w.writeheader(); w.writerows(rows)
    bank_store._load_city.cache_clear()  # so the new bank is picked up


def _lab(v):
    v = str(v or "UNKNOWN").upper()
    return v if v in {"EXCELLENT", "GOOD", "TOUGH", "FAIL", "UNKNOWN"} else "UNKNOWN"


def _bank_rows_from_guide(city: str, places: list, contract_stub: dict, user_id) -> list:
    """Path A: per-place facts extracted from retrieved GUIDE text."""
    _FACT_PROMPT = (
        "From the guide text, rate this place's accessibility. Use ONLY the text; "
        "if it says nothing about a dimension, use UNKNOWN. Return ONLY JSON: "
        '{"wheelchair":<EXCELLENT|GOOD|TOUGH|FAIL|UNKNOWN>,'
        '"toddler":<...>,"senior":<...>,"note":<one sentence from the text>}.\n\n'
        "Place: {name}\nGuide text:\n\"\"\"{ev}\"\"\""
    )
    rows, seen = [], set()
    for p in places:
        name = (p.get("name") or "").strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        ev = _gather_evidence(p, contract_stub)
        try:
            out = gateway.chat_json("clarify",
                [{"role": "user", "content": _FACT_PROMPT.format(name=name, ev=ev[:1200])}],
                user_id=user_id)
        except Exception:
            out = {}
        rows.append({"city": city, "place": name,
                     "is_famous": bool(p.get("is_famous")),
                     "wheelchair": _lab(out.get("wheelchair")),
                     "toddler": _lab(out.get("toddler")),
                     "senior": _lab(out.get("senior")),
                     "confidence": "LOW",   # guide-derived at runtime -> low confidence
                     "note": (out.get("note") or "").strip(),
                     "source": "runtime guide extraction"})
    return rows


def _bank_rows_from_llm_knowledge(city: str, user_id) -> list:
    """Path B: out-of-corpus city. Seed a bank from the model's own knowledge.

    Ratings are REAL labels (the model knows the Louvre has lifts, that a
    hilltop fort has stairs) but the whole batch is confidence=LOW and
    source="llm knowledge" so it's honestly marked as unverified on this first
    generation - and softens rather than hard-blocks until replaced.
    """
    _KNOWLEDGE_PROMPT = (
        "You are seeding an accessibility guide for {city}. "
        "FIRST decide: is \"{city}\" a real, identifiable city or place you know? "
        "If it is NOT a real place you recognize (a made-up or unrecognizable name), "
        "return exactly {{\"places\":[],\"unknown_place\":true}} and nothing else. "
        "Otherwise, list up to 10 of the "
        "most famous must-see attractions there. For each, rate its accessibility "
        "from real-world knowledge (ramps, lifts, step-free entry, stairs, terrain). "
        "If you are genuinely unsure about a dimension for a place, use UNKNOWN for "
        "that dimension only. Return ONLY JSON: "
        '{{"places":[{{"name":<short name>,"is_famous":true,'
        '"wheelchair":<EXCELLENT|GOOD|TOUGH|FAIL|UNKNOWN>,'
        '"toddler":<...>,"senior":<...>,'
        '"note":<one sentence on its accessibility>}}]}}'
    )
    try:
        out = gateway.chat_json("clarify",
            [{"role": "user", "content": _KNOWLEDGE_PROMPT.format(city=city)}],
            user_id=user_id)
        items = out.get("places", []) or []
    except Exception:
        items = []

    rows, seen = [], set()
    for it in items:
        name = (it.get("name") or "").strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        rows.append({"city": city, "place": name,
                     "is_famous": bool(it.get("is_famous", True)),
                     "wheelchair": _lab(it.get("wheelchair")),
                     "toddler": _lab(it.get("toddler")),
                     "senior": _lab(it.get("senior")),
                     "confidence": "LOW",           # unverified -> softens, never hard-blocks
                     "note": (it.get("note") or "").strip(),
                     "source": "llm knowledge"})    # flagged as model-derived on first build

    # ONBOARDING AGENT (true agent): upgrade the LLM's guessed ratings with REAL
    # evidence. Geocode each candidate, then let the agent investigate via OSM
    # tools and record evidence-based verdicts. Where it verifies a place, we
    # replace the "llm knowledge / LOW" row with the agent's grounded rating.
    try:
        from app.tools import travel_data as _td
        from app.agents.tool_agents import onboarding_agent
        candidates = []
        for r in rows:
            coords = _td.geocode_place(r["place"], city)
            if coords:
                candidates.append({"name": r["place"], "lat": coords[0], "lng": coords[1]})
        if candidates:
            verdict = onboarding_agent(city, candidates, user_id=user_id)
            by_name = {(v.get("place") or "").lower(): v
                       for v in (verdict.get("ratings") or [])}
            for r in rows:
                v = by_name.get(r["place"].lower())
                if v and (v.get("confidence") or "").upper() != "LOW":
                    # agent verified this place with real evidence -> use its rating
                    r["wheelchair"] = _lab(v.get("wheelchair", r["wheelchair"]))
                    r["toddler"] = _lab(v.get("toddler", r["toddler"]))
                    r["senior"] = _lab(v.get("senior", r["senior"]))
                    r["confidence"] = (v.get("confidence") or "LOW").upper()
                    r["note"] = (v.get("note") or r["note"]).strip()
                    r["source"] = "onboarding agent (OSM-verified)"
    except Exception as e:
        print(f"[bank] onboarding agent skipped: {e}")

    return rows


@observe(name="constraint_plan_pipeline")
def _day_center(day: dict):
    """Mean lat/lng of a day's placed stops, for a meal-search center."""
    pts = []
    for slot in ("morning", "afternoon", "evening"):
        b = (day.get("blocks") or {}).get(slot)
        if b and b.get("lat") is not None and b.get("lng") is not None:
            pts.append((b["lat"], b["lng"]))
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _meal_pick(places: list, kind_label: str):
    """Shape a places-tool result into {best, alternatives} for one meal."""
    out = []
    for p in places:
        bits = []
        if p.get("cuisine"):
            bits.append(p["cuisine"].replace(";", ", "))
        access = p.get("access")
        tag = {"yes": "step-free", "limited": "step-free (limited)",
               "designated": "step-free"}.get(access, access or "")
        out.append({"name": p["name"], "cuisine": p.get("cuisine"),
                    "access": access, "access_label": tag,
                    "address": p.get("address"),
                    "changing_table": bool(p.get("changing_table")),
                    "highchair": bool(p.get("highchair"))})
    if not out:
        return None
    return {"meal": kind_label, "best": out[0], "alternatives": out[1:3]}


def _enrich_meals(itinerary: dict, contract: dict, user_id=None) -> None:
    """Attach lunch + dinner picks to each day, grounded in real accessible places
    near that day's stops. Mutates itinerary in place. Fully network-guarded:
    any failure just leaves meals off that day rather than breaking the plan.
    """
    try:
        from app.tools import travel_data
        from app.config import get_settings
        if not get_settings().enable_places_tool:
            return
    except Exception:
        return
    # which access filter matches the traveler
    constraint = "wheelchair"
    travelers = contract.get("travelers") or []
    if any((t or {}).get("type") == "toddler" for t in travelers):
        constraint = "stroller"
    if any((t or {}).get("mobility") == "wheelchair" for t in travelers):
        constraint = "wheelchair"

    # cuisines that aren't real lunch/dinner spots (dessert, coffee, snacks)
    _NON_MEAL = {"ice_cream", "coffee_shop", "cafe", "dessert", "donut",
                 "bubble_tea", "juice", "bakery"}
    used_names = set()  # de-dupe across days

    # trip-wide fallback center: mean of every day that DOES have coords, so a
    # day whose single stop lacks a geocode still gets nearby meal suggestions.
    all_centers = [c for c in (_day_center(d) for d in (itinerary.get("days") or [])) if c]
    trip_center = None
    if all_centers:
        trip_center = (sum(c[0] for c in all_centers) / len(all_centers),
                       sum(c[1] for c in all_centers) / len(all_centers))

    def _is_meal(p):
        cui = (p.get("cuisine") or "").lower()
        return not any(nm in cui for nm in _NON_MEAL)

    for day in (itinerary.get("days") or []):
        center = _day_center(day) or trip_center
        if not center:
            continue
        lat, lng = center
        try:
            # pull a wider set so we can filter to real meals + de-dupe
            res = travel_data.accessible_places(lat, lng, kind="food",
                                                constraint=constraint, limit=14)
            places = res.get("places", [])
        except Exception:
            places = []
        # prefer actual restaurants, drop dessert/coffee, drop already-used
        meal_places = [p for p in places
                       if p.get("amenity") == "restaurant" and _is_meal(p)
                       and p["name"] not in used_names]
        # if too few restaurants, allow other food places (still meal-appropriate)
        if len(meal_places) < 4:
            meal_places += [p for p in places
                            if _is_meal(p) and p["name"] not in used_names
                            and p not in meal_places]
        meals = []
        if meal_places:
            lunch = _meal_pick(meal_places[:3], "Lunch")
            dinner = _meal_pick(meal_places[3:6] or meal_places[:3], "Dinner")
            if lunch:
                meals.append(lunch)
                used_names.update(a["name"] for a in
                                  [lunch["best"]] + lunch.get("alternatives", []))
            if dinner:
                meals.append(dinner)
                used_names.update(a["name"] for a in
                                  [dinner["best"]] + dinner.get("alternatives", []))
        elif res.get("kind") == "sparse":
            day["meals_note"] = ("Few places here have verified accessible dining "
                                 "data \u2014 ask your hotel or call ahead.")
        day["meals"] = meals


def _plan_grounding(rated: list) -> dict:
    """Detect whether the plan is guide-grounded or LLM-knowledge (unverified).

    Out-of-corpus cities fall back to model knowledge (source="llm knowledge",
    confidence LOW). We surface that honestly so the UI can warn that access
    details are unverified and coordinates may be approximate.
    """
    if not rated:
        return {"level": "none", "note": None}
    llm = 0; total = 0
    for r in rated:
        for c in (r.get("per_constraint") or {}).values():
            total += 1
            cite = (c.get("citation") or "")
            basis = (c.get("basis") or "")
            if "llm knowledge" in str(cite).lower() or basis in ("unknown",):
                llm += 1
    if total and llm / total > 0.6:
        return {"level": "llm",
                "note": ("Heads up: this city isn't in our vetted guide corpus yet, "
                         "so accessibility details come from the model's general "
                         "knowledge (unverified) and map pins may be approximate. "
                         "Confirm step-free access directly before you rely on it.")}
    return {"level": "guide", "note": None}


def plan_trip(request: str, user_id: str | None = None, use_cache: bool = True,
              with_meals: bool = True, progress=None) -> dict:
    """Full chain: request -> contract -> retrieve -> rate -> assemble.

    Returns {contract, chips, activities_rated, itinerary, needs_clarification,
             clarification_question}.

    use_cache=False bypasses the semantic cache entirely (read AND write) - used
    by evals so they test the live pipeline logic, never a stale cached plan.
    """
    # Semantic cache: an identical/near-identical plan request skips the whole
    # pipeline (contract extract + retrieval + per-place LLM rating + assemble),
    # which is the slow part - rating is one LLM call per place. Same pattern the
    # /ask graph uses; keyed on the raw request text with a "plan::" prefix.
    #
    # GUARD: the cache index is shared with /ask, and a raw k-NN lookup can return
    # the nearest *Q&A* answer for the same city (which has no itinerary). Only
    # accept a hit that is actually a plan result AND whose cached query was a plan.
    # Build a CONSTRAINT-AWARE cache key. The semantic cache would otherwise
    # fuzzy-match "Rome with a toddler" to a cached "Rome with a wheelchair" plan
    # (same city, close embedding) and serve the wrong constraints. We append the
    # distinctive constraint tokens found in the raw request so different-constraint
    # trips get distinct keys.
    import re as _re0
    _ctoks = []
    for _kw in ["wheelchair", "toddler", "stroller", "senior", "elderly",
                "vegetarian", "vegan", "halal", "kosher", "gluten", "budget"]:
        if _re0.search(rf"\b{_kw}", request, _re0.I):
            _ctoks.append(_kw)
    _cache_key = "plan::" + request + "::" + ",".join(sorted(_ctoks))

    # If the trip has ANY access/dietary/budget constraint, skip the semantic
    # cache entirely. The semantic cache matches on embedding similarity, which
    # can't reliably tell "Rome with a toddler" from "Rome with a wheelchair" -
    # so caching constraint trips risks serving the WRONG constraints. Constraint
    # trips are the whole point of this product; correctness beats the cache here.
    _has_constraints = bool(_ctoks)
    if _has_constraints:
        use_cache = False

    if use_cache:
        try:
            from app.stores.cache import lookup as _cache_lookup
            _cached = _cache_lookup(_cache_key)
            _is_plan_hit = (
                isinstance(_cached, dict)
                and _cached.get("itinerary")
                and (_cached.get("_cache", {}).get("cached_query", "")).startswith("plan::")
                # the cached plan's constraint tokens must EXACTLY match this request's
                and (_cached.get("_cache", {}).get("cached_query", "")).endswith(
                    "::" + ",".join(sorted(_ctoks)))
            )
            if _is_plan_hit:
                _cached["_cache_hit"] = True
                return _cached
        except Exception:
            pass

    # Phase 1: extract the contract (+ detected/suggested chips)
    def _p(stage, label):
        if progress:
            try: progress(stage, label)
            except Exception: pass
    _p("extract", "Understanding your constraints")
    state = {"query": request, "thread_id": "plan"}
    ex = extract_requirements(state)
    contract = ex["constraints"]
    chips = {"detected": ex["detected_constraints"],
             "suggested": ex["suggested_constraints"]}

    # Required-field guard (code-enforced, not LLM-trusted): destination AND
    # trip length are both required. The LLM sets needs_clarification, but it
    # sometimes proceeds with a null trip length - so we check deterministically
    # here too and ask, rather than assembling an empty/degenerate plan.
    _dest = contract.get("destination")
    _days = contract.get("trip_length_days")
    # Safety net: the LLM sometimes "helpfully" defaults a trip length the user
    # never gave. A constraint-faithful planner must not invent it. If the raw
    # request contains no explicit day/week signal, force days back to null so
    # the guard below asks rather than fabricating.
    import re as _re
    if _days and not _re.search(r"\b(\d+\s*(day|days|night|nights|week|weeks)|a\s+week|weekend|fortnight)\b",
                                request, _re.I):
        _days = None
        contract["trip_length_days"] = None
    if ex.get("needs_clarification") or not _dest or not _days:
        # Order matters. Handle the cases from most-specific to least:
        #   1. destination missing entirely            -> ask which city
        #   2. destination present but days missing     -> ask how many days
        #      (a real city like "Rome" with no day count lands here - do NOT
        #       mistake it for an unrecognized place just because the LLM raised
        #       needs_clarification about the missing days)
        #   3. destination present, days present, but the LLM still flags
        #      clarification -> it doesn't recognize the place (e.g. "Zputnik")
        #      -> "couldn't find it" with suggestions
        import re, random
        corpus = _corpus_cities()

        if not _dest:
            return {"contract": contract, "chips": chips,
                    "needs_clarification": True,
                    "clarification_question": "Which city are you visiting?",
                    "activities_rated": [], "itinerary": None}

        if not _days:
            return {"contract": contract, "chips": chips,
                    "needs_clarification": True,
                    "clarification_question": f"How many days is your {_dest} trip?",
                    "activities_rated": [], "itinerary": None}

        # dest + days both present, yet the LLM asked to clarify -> unknown place
        if ex.get("needs_clarification"):
            sugg = _closest_city(_dest, corpus)
            reason = (f"I couldn't find travel data for \u201c{_dest}\u201d."
                      + (f" Did you mean {sugg}?" if sugg and sugg.lower() != _dest.lower()
                         else " Could you try a major city?"))
            return {"contract": contract, "chips": chips,
                    "needs_clarification": False,
                    "empty_reason": reason,
                    "empty_suggestions": random.sample(corpus, min(6, len(corpus))) if corpus else [],
                    "activities_rated": [], "itinerary": None}

    # Retrieve real section-chunks for the destination
    # Activity sourcing (Fix A): the BANK is the authoritative catalog of a
    # city's real places, so seed candidates from it directly. Retrieval +
    # decomposition then SUPPLEMENT with any places not yet in the bank (so new
    # spots still get discovered), rather than being the sole source - which was
    # surfacing vague neighborhood names instead of the real attractions.
    from app.stores import bank as bank_store

    # Lazy-fill: if this city has no bank yet, build+cache one now (read-through).
    # Corpus cities ship with curated banks; anything new self-warms on first ask.
    if not bank_store.has_bank(contract.get("destination") or ""):
        _lazy_build_bank(contract.get("destination") or "", user_id=user_id)

    bank_places = bank_store.list_places(contract.get("destination") or "")

    _p("retrieve", "Finding accessible places")
    # supplement: retrieve + decompose, keep only places NOT already in the bank
    chunks = _retrieve_activities(contract)
    extracted = decompose_all(chunks, user_id=user_id)
    seen_names = {(_norm_name(a["name"])) for a in bank_places}
    # backstop against action/ritual duplicates (e.g. "Coin Throwing" carrying
    # the Trevi Fountain note): if a supplemental item's evidence text is
    # byte-identical to a bank place's note, it's the same place surfaced under
    # an action name -> drop it. The decomposer prompt already excludes actions;
    # this guarantees the specific inherited-note case can't recur.
    bank_notes = {(a.get("text") or "").strip() for a in bank_places if (a.get("text") or "").strip()}
    supplemental = []
    for a in extracted:
        nm = _norm_name(a.get("name", ""))
        # skip if it's already a bank place (exact-ish name) ...
        if nm in seen_names:
            continue
        # ... or if it fuzzily/LLM-resolves to a bank place (e.g. "Old Town" ->
        # "Old Town Square", "New Town" -> "Wenceslas Square"): those are the
        # same place surfaced by retrieval under a looser name, so drop them.
        resolved, _how = bank_store.resolve(a.get("name", ""),
                                            contract.get("destination") or "",
                                            allow_llm=False)
        if resolved:
            continue
        # drop if this item's decomposed text is byte-identical to a bank note
        # (an action/ritual that inherited a real place's description)
        if (a.get("text") or "").strip() in bank_notes:
            continue
        # genuinely new (non-bank) place: gather prose evidence and keep it
        a["text"] = _gather_evidence(a, contract)
        supplemental.append(a)

    activities = bank_places + supplemental

    # Phase 2: rate each activity against the contract (bank places rate from the
    # bank; supplemental places fall back to prose inside rate_activity)
    _p("rate", "Checking each place against your needs")
    rated = [rate_activity(a, contract, user_id=user_id) for a in activities]
    # carry decomposition metadata (name, is_famous, section_hint) onto the rating
    for r, a in zip(rated, activities):
        r["activity"]["name"] = a.get("name")
        r["activity"]["is_famous"] = a.get("is_famous", False)
        r["activity"]["section_hint"] = a.get("section_hint")

    # Phase 3: assemble into day plan + skipped + critique
    _p("assemble", "Arranging your days")
    itinerary = assemble_itinerary(rated, contract)

    # Phase 3.5: meal enrichment - attach lunch + dinner picks per day, grounded in
    # real accessible places near that day's stops. Opt-in (off during eval) so the
    # deterministic core stays fast; network-guarded so it never breaks a plan.
    if with_meals:
        _p("meals", "Adding nearby accessible dining")
        _enrich_meals(itinerary, contract, user_id)

    # Empty-plan guard: if retrieval AND the lazy bank both found nothing real for
    # this destination, the plan comes back with 0 placed and 0 skipped. That's
    # the signal the city couldn't be sourced (a typo like "room", or a place we
    # genuinely have no data for) - NOT a normal result. Offer a "did you mean?"
    # hint if the destination is close to a corpus city. This runs AFTER the lazy
    # bank has had its chance, so real new cities that self-warm never reach here.
    empty_reason = None
    empty_suggestions = []
    days = (itinerary or {}).get("days", [])
    placed = sum(1 for d in days for s in ("morning", "afternoon", "evening") if d.get("blocks", {}).get(s))
    skipped_n = len((itinerary or {}).get("skipped", []))
    if placed == 0 and skipped_n == 0:
        dest = contract.get("destination") or "that destination"
        corpus = _corpus_cities()
        suggestion = _closest_city(dest, corpus)
        if suggestion and suggestion.lower() != dest.lower():
            empty_reason = (f"I couldn't find travel data for \u201c{dest}\u201d. "
                            f"Did you mean {suggestion}?")
        else:
            empty_reason = (f"I couldn't build a plan for \u201c{dest}\u201d \u2014 I don't have "
                            f"enough grounded travel data for it yet. Could you try a "
                            f"specific city, or tell me more about where you mean?")
        # always offer a concrete next step: a few cities we DO cover well
        import random
        empty_suggestions = random.sample(corpus, min(6, len(corpus))) if corpus else []

    result = {
        "contract": contract,
        "chips": chips,
        "needs_clarification": False,
        "clarification_question": None,
        "activities_rated": rated,
        "itinerary": itinerary,
        "empty_reason": empty_reason,
        "empty_suggestions": empty_suggestions,
        "grounding": _plan_grounding(rated),
    }
    # cache the finished plan so a repeat/near-identical request is instant
    # (skip caching an empty/failed plan so a fixed corpus later can succeed)
    if use_cache and not empty_reason:
        try:
            from app.stores.cache import store as _cache_store
            _cache_store(_cache_key, result)
        except Exception:
            pass
    return result
