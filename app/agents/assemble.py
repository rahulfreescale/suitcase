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

    # 1. a hard FAIL is the most decisive reason
    hard_fails = [(k, v) for k, v in per.items() if v.get("hard") and v["label"] == "FAIL"]
    if hard_fails:
        return _text(hard_fails[0][1])
    # 2. a hard constraint (wheelchair/budget) that isn't a clear pass owns the reason
    for k in ("wheelchair", "budget"):
        v = per.get(k)
        if v and v["label"] in ("FAIL", "TOUGH", "UNKNOWN"):
            return _text(v)
    # 3. otherwise the lowest-scoring constraint
    return _text(min(per.items(), key=lambda kv: kv[1]["score"])[1])


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
        days[d][b] = {
            "name_hint": rated["activity"].get("name") or rated["activity"].get("text", "")[:80],
            "city": rated["activity"].get("city"),
            "page": rated["activity"].get("page"),
            "overall": rated["overall"],
            "per_constraint": rated["per_constraint"],
        }
    leftover_good = good[len(slots):]  # more good-fits than slots (fine; extras)

    # 3. skipped list: poor-fit places, origin-tagged.
    #    Famous poor-fits are the stars of this section ("popular spots left
    #    out"). Guide "Popular but Challenging" entries get origin=guide_flagged.
    skipped = []
    for rated in poor:
        act = rated["activity"]
        section = (act.get("section_hint") or "")
        is_guide_flagged = "Popular but Challenging" in section
        skipped.append({
            "name_hint": act.get("name") or act.get("text", "")[:80],
            "city": act.get("city"),
            "page": act.get("page"),
            "is_famous": act.get("is_famous", False),
            "overall": rated["overall"],
            "per_constraint": rated.get("per_constraint", {}),
            "reason": _fail_reason(rated),
            "origin": "guide_flagged" if is_guide_flagged else "rating",
            "alternative": None,
        })
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
        day = {
            "day": d,
            "day_fit": days[d]["_day_fit"],
            "blocks": {b: days[d][b] for b in _BLOCKS},
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
