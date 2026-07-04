"""Phase 3 - Itinerary assembly + critic.

Takes a list of RATED activities (Phase 2 applied to everything retrieved for a
city) and the constraint contract, and produces the structured plan:

  1. SORT      good-fits vs poor-fits by overall score.
  2. PLACE     good-fits into fixed day blocks: morning / afternoon / evening,
               across trip_length_days from the contract.
  3. SKIP      poor-fit FAMOUS places -> the "left out, and why" list, each with
               a reason + (if available) an alternative, tagged by ORIGIN:
                 - "rating"       : landed here because it scored TOUGH/FAIL
                 - "guide_flagged": came from a guide "Popular but Challenging"
                   entry. (Slot ready; populated once ingest tags chunks by
                   section - see project note. Today only "rating" is produced.)
  4. ROLLUP    each day gets a day-fit % (mean of its activities' scores). A
               required hard FAIL anywhere drags a day down hard.
  5. CRITIC    flag weak days (too few good-fits, or a required violation slipped
               in) for a capped re-plan. This is the generator-critic signal.

This module is pure assembly logic over already-rated items - no LLM call - so
it's fast, deterministic, and easy to test.
"""
from app.observability import observe

_BLOCKS = ["morning", "afternoon", "evening"]

# Score thresholds
_GOOD_FIT_MIN = 55      # overall score >= this -> eligible for the itinerary
_WEAK_DAY_MIN = 55      # day-fit % below this -> critic flags the day
_MIN_GOOD_PER_DAY = 2   # fewer good-fits than blocks*days may leave gaps


def _haversine_ish(a: dict, b: dict) -> float:
    """Cheap planar distance between two placed blocks with lat/lng.

    Not true great-circle distance - within a single city the flat approximation
    (scaling longitude by cos(lat) so a degree of lng shrinks toward the poles)
    is more than accurate enough to order 2-3 stops, and it's instant.
    """
    import math
    dlat = a["lat"] - b["lat"]
    dlng = (a["lng"] - b["lng"]) * math.cos(math.radians(a["lat"]))
    return math.hypot(dlat, dlng)


def _order_day_geographically(day_blocks: dict) -> dict:
    """Reorder a day's morning/afternoon/evening so the route doesn't zig-zag.

    Keeps the same PLACES on the same DAY (day assignment is untouched) - it only
    resequences within the day. Anchors on whatever fills the morning slot, then
    picks the nearest remaining stop at each step (nearest-neighbor). With 2-3
    stops this is effectively optimal. Blocks are then relabeled in travel order
    so the first stop is 'morning', etc.

    Falls back to the original order if any placed stop lacks coordinates - we
    never guess a route we can't ground.
    """
    placed = [(b, day_blocks[b]) for b in _BLOCKS if day_blocks.get(b)]
    if len(placed) < 2:
        return day_blocks
    stops = [blk for _, blk in placed]
    if any(("lat" not in s or "lng" not in s) for s in stops):
        return day_blocks  # missing coords -> leave as-is, don't fabricate a route

    remaining = stops[:]
    ordered = [remaining.pop(0)]  # anchor = current morning stop
    while remaining:
        last = ordered[-1]
        nxt = min(range(len(remaining)),
                  key=lambda i: _haversine_ish(last, remaining[i]))
        ordered.append(remaining.pop(nxt))

    # relabel into travel order across the slots this day actually had filled
    filled_slots = [b for b, _ in placed]
    new_blocks = {b: None for b in _BLOCKS}
    for slot, stop in zip(filled_slots, ordered):
        new_blocks[slot] = stop
    return new_blocks


def _is_good_fit(rated: dict) -> bool:
    """Good enough to place in the plan: no hard FAIL, decent overall."""
    per = rated.get("per_constraint", {})
    hard_fail = any(v.get("hard") and v.get("label") == "FAIL" for v in per.values())
    return (not hard_fail) and rated["overall"]["score"] >= _GOOD_FIT_MIN


def _fail_reason(rated: dict) -> str:
    """Human-readable reason a place was skipped (from its worst *relevant* constraint).

    Ties matter: a wheelchair problem and a toddler problem can both score 35, and
    a naive min() would surface whichever constraint sorts first in the dict (often
    the wrong one). So we prefer, in order: a hard FAIL, then the hard constraint
    (wheelchair/budget) if it's not a clear pass, then the lowest score.

    We return the citation text as-is WITHOUT a "constraint:" prefix - prefixing a
    negative reason with "toddler-friendly:" reads as if the place *is* toddler-
    friendly, which fights the TOUGH/FAIL tag. The tag already gives the verdict;
    this gives the why.
    """
    per = rated.get("per_constraint", {})
    if not per:
        return "Doesn't fit the constraints well enough for this trip."

    def _text(v):
        c = (v.get("citation") or "").strip()
        return c or {"FAIL": "Fails a required need for this trip.",
                     "TOUGH": "Doable but difficult given the constraints.",
                     "UNKNOWN": "Not enough accessibility info to place confidently."
                    }.get(v.get("label"), "Limited fit for this trip.")

    # Pick the constraint that owns the reason, and remember WHICH one it is so we
    # can sanitize borrowed wording below.
    def _pick():
        # 1. a hard FAIL is the most decisive reason
        hard_fails = [(k, v) for k, v in per.items() if v.get("hard") and v["label"] == "FAIL"]
        if hard_fails:
            return hard_fails[0]
        # 2. any ACTIVE hard constraint that isn't a clear pass owns the reason. We key
        # off v["hard"] (present only for constraints the traveler actually requested)
        # rather than a fixed wheelchair/budget list - otherwise a toddler-only trip
        # would still surface a wheelchair reason. Lowest-scoring hard constraint wins.
        hard_soft = [(k, v) for k, v in per.items()
                     if v.get("hard") and v["label"] in ("FAIL", "TOUGH", "UNKNOWN")]
        if hard_soft:
            return min(hard_soft, key=lambda kv: kv[1]["score"])
        # 3. otherwise the lowest-scoring constraint (of whatever IS active)
        return min(per.items(), key=lambda kv: kv[1]["score"])

    key, v = _pick()
    return _sanitize_reason(_text(v), key, per)


# Wording that describes wheelchair access specifically. When a reason is chosen
# for a NON-wheelchair constraint (toddler/senior) but the underlying note/citation
# was written in wheelchair terms - common because bank notes and guide prose phrase
# accessibility around wheelchairs - we rewrite it to the obstacle that actually
# matters for that traveler. This is a deterministic backstop that works no matter
# where the text came from (bank note, lazy-build, or LLM prose).
_WHEELCHAIR_PHRASES = [
    "limited wheelchair accessibility to main buildings",
    "limited wheelchair accessibility in main shopping area",
    "limited wheelchair accessibility",
    "not wheelchair accessible",
    "no wheelchair access",
    "wheelchair accessibility is limited",
    "wheelchair access is limited",
    "difficult for wheelchairs",
    "hard for wheelchairs",
    "not accessible for wheelchairs",
    "wheelchair users",
    "for wheelchair users",
    "in a wheelchair",
]

def _sanitize_reason(text: str, chosen_key: str, per: dict) -> str:
    """Strip wheelchair-specific wording from a reason chosen for a non-wheelchair need.

    We only touch the text when (a) the reason was NOT chosen for the wheelchair
    constraint and (b) wheelchair is not even an active constraint for this trip -
    so we never alter a legitimate wheelchair reason. The verdict (TOUGH/FAIL) and
    the concrete obstacles (stairs, unpaved paths, uneven terrain) are preserved;
    only the trailing wheelchair clause is removed or, if nothing else remains, a
    need-appropriate fallback is used.
    """
    if chosen_key == "wheelchair" or "wheelchair" in per:
        return text
    if not text or "wheelchair" not in text.lower():
        return text

    cleaned = text
    for phrase in _WHEELCHAIR_PHRASES:
        # remove the phrase and a leading connector like "; " or ", " if present
        for connector in ("; ", ", ", " - ", ". ", " "):
            cleaned = cleaned.replace(connector + phrase, "")
            cleaned = cleaned.replace(connector + phrase.capitalize(), "")
        cleaned = cleaned.replace(phrase, "")
        cleaned = cleaned.replace(phrase.capitalize(), "")
    # tidy leftover punctuation/space
    cleaned = cleaned.strip().strip(";,").strip()
    if cleaned and not cleaned.endswith("."):
        cleaned += "."

    # If we stripped everything meaningful, fall back to a need-appropriate line.
    if len(cleaned) < 15:
        if chosen_key == "toddler-friendly":
            return "Stairs and uneven or unpaved terrain make it difficult with a stroller."
        if chosen_key == "senior-friendly":
            return "Stairs, steep or uneven terrain and long walking make it tiring."
        return "Doable but difficult given the constraints."
    return cleaned


def assemble_itinerary(rated_activities: list[dict], contract: dict) -> dict:
    """Assemble rated activities into a day-by-day plan + skipped list + critique.

    rated_activities: list of Phase 2 rate_activity() outputs.
    Returns {days: [...], skipped: [...], critique: {...}}
    """
    days_count = contract.get("trip_length_days") or 1
    days_count = max(1, int(days_count))

    # 1. sort by overall score, best first
    ranked = sorted(rated_activities, key=lambda r: r["overall"]["score"], reverse=True)

    good = [r for r in ranked if _is_good_fit(r)]
    poor = [r for r in ranked if not _is_good_fit(r)]

    # 2. place good-fits into morning/afternoon/evening across the days
    slots = [(d, b) for d in range(1, days_count + 1) for b in _BLOCKS]  # day-major order
    days = {d: {b: None for b in _BLOCKS} for d in range(1, days_count + 1)}
    for rated, (d, b) in zip(good, slots):
        act = rated["activity"]
        block = {
            "name_hint": act.get("name") or act.get("text", "")[:80],
            "city": act.get("city"),
            "page": act.get("page"),
            "overall": rated["overall"],
            "per_constraint": rated["per_constraint"],
        }
        if act.get("lat") is not None and act.get("lng") is not None:
            block["lat"], block["lng"] = act["lat"], act["lng"]
        days[d][b] = block
    leftover_good = good[len(slots):]  # more good-fits than slots (fine; extras)

    # 3. skipped list: poor-fit places, origin-tagged.
    #    Famous poor-fits are the stars of this section ("popular spots left
    #    out"). Guide "Popular but Challenging" entries get origin=guide_flagged.
    skipped = []
    for rated in poor:
        act = rated["activity"]
        section = (act.get("section_hint") or "")
        is_guide_flagged = "Popular but Challenging" in section
        sk = {
            "name_hint": act.get("name") or act.get("text", "")[:80],
            "city": act.get("city"),
            "page": act.get("page"),
            "is_famous": act.get("is_famous", False),
            "overall": rated["overall"],
            "per_constraint": rated.get("per_constraint", {}),
            "reason": _fail_reason(rated),
            "origin": "guide_flagged" if is_guide_flagged else "rating",
            "alternative": None,
        }
        if act.get("lat") is not None and act.get("lng") is not None:
            sk["lat"], sk["lng"] = act["lat"], act["lng"]
        skipped.append(sk)
    # surface famous ones first - they're what the user recognizes and cares about
    skipped.sort(key=lambda s: (not s["is_famous"], s["overall"]["score"]))

    # 4. rollup: day-fit % = mean of placed activities' scores
    for d in range(1, days_count + 1):
        placed = [b for b in days[d].values() if b]
        if placed:
            day_fit = round(sum(b["overall"]["score"] for b in placed) / len(placed))
        else:
            day_fit = 0
        days[d]["_day_fit"] = day_fit
        days[d]["_placed_count"] = len(placed)

    # 5. critic: flag weak days for re-plan
    weak_days = []
    for d in range(1, days_count + 1):
        fit = days[d]["_day_fit"]
        cnt = days[d]["_placed_count"]
        if cnt < _MIN_GOOD_PER_DAY or (cnt > 0 and fit < _WEAK_DAY_MIN):
            weak_days.append({"day": d, "day_fit": fit, "placed": cnt,
                              "issue": ("too few good-fit activities" if cnt < _MIN_GOOD_PER_DAY
                                        else "day-fit below threshold")})
    needs_replan = len(weak_days) > 0

    # shape days for output (ordered list, drop the private keys into fields)
    days_out = []
    total_slots = days_count * len(_BLOCKS)
    total_placed = sum(days[d]["_placed_count"] for d in range(1, days_count + 1))
    for d in range(1, days_count + 1):
        placed_n = days[d]["_placed_count"]
        empty_n = len(_BLOCKS) - placed_n
        ordered_blocks = _order_day_geographically({b: days[d][b] for b in _BLOCKS})
        day = {
            "day": d,
            "day_fit": days[d]["_day_fit"],
            "blocks": ordered_blocks,
            "placed_count": placed_n,
            "empty_count": empty_n,
        }
        days_out.append(day)

    # honest fill note: if we couldn't fill the plan, say why (not enough good-fit
    # places for these constraints) rather than leaving blank cards looking broken.
    fill_note = None
    if total_placed < total_slots:
        good_total = len(good)
        fill_note = (
            f"Only {good_total} spot{'s' if good_total != 1 else ''} met the bar for "
            f"these constraints, so some slots are left open. That's deliberate — the "
            f"rest are in “left out” below, with the specific reason each doesn't fit. "
            f"A padded plan would defeat the point."
        )

    return {
        "days": days_out,
        "skipped": skipped,
        "leftover_good_count": len(leftover_good),
        "fill_note": fill_note,
        "critique": {
            "needs_replan": needs_replan,
            "weak_days": weak_days,
            "good_fit_count": len(good),
            "poor_fit_count": len(poor),
        },
    }


@observe(name="assemble_itinerary")
def assemble_node(state):
    """Graph-node wrapper (used in Phase 4 wiring)."""
    rated = state.get("rated_activities", [])
    contract = state.get("constraints", {})
    plan = assemble_itinerary(rated, contract)
    return {**state, "itinerary": plan}
