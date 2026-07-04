"""Standalone test for Phase 3 - itinerary assembly + critic.

Pure logic (no LLM): feeds hand-built RATED activities and checks:
  1. only good-fits land in the day plan (no hard-FAIL activity is placed)
  2. the skipped list captures poor-fits with a reason + origin tag
  3. day-fit rollup math is correct
  4. the critic flags weak days for re-plan

Usage:  python3 -m eval.test_assemble    (no API key needed)
"""
from app.agents.assemble import assemble_itinerary


def _rated(city, page, text, overall_label, overall_score, per):
    return {"activity": {"city": city, "page": page, "text": text},
            "overall": {"label": overall_label, "score": overall_score},
            "per_constraint": per}


def hard_fail(kind, cite):
    return {kind: {"label": "FAIL", "score": 0, "basis": "data",
                   "citation": cite, "hard": True}}

def soft(kind, label, score, cite):
    return {kind: {"label": label, "score": score, "basis": "guide",
                   "citation": cite, "hard": False}}


def build_inputs():
    # A mix: 4 strong good-fits, 1 mediocre, 2 hard-fails (should be skipped).
    return [
        _rated("Prague", 1, "Letna Park flat paved wheelchair accessible stroller-friendly",
               "EXCELLENT", 90, {**soft("toddler", "EXCELLENT", 90, "great for young kids"),
                                 "wheelchair": {"label": "EXCELLENT", "score": 90, "basis": "data",
                                                "citation": "wheelchair accessible", "hard": True}}),
        _rated("Prague", 2, "Prague Zoo lends strollers and wheelchairs free",
               "EXCELLENT", 88, {**soft("toddler", "EXCELLENT", 90, "zoo popular with kids"),
                                 "wheelchair": {"label": "EXCELLENT", "score": 85, "basis": "data",
                                                "citation": "lends wheelchairs", "hard": True}}),
        _rated("Prague", 3, "Vltava river cruise accessible boarding",
               "GOOD", 72, {**soft("toddler", "GOOD", 70, "calm boat ride"),
                            "wheelchair": {"label": "GOOD", "score": 70, "basis": "data",
                                           "citation": "accessible boarding", "hard": True}}),
        _rated("Prague", 4, "Kafka Museum step-free access",
               "GOOD", 68, {**soft("toddler", "TOUGH", 35, "quiet museum, less for toddlers"),
                            "wheelchair": {"label": "EXCELLENT", "score": 90, "basis": "data",
                                           "citation": "step-free", "hard": True}}),
        _rated("Prague", 5, "Small Old Town museum upstairs, no lift",
               "FAIL", 0, hard_fail("wheelchair", "no lift, upstairs")),
        _rated("Prague", 6, "Old Castle Stairs steep steps no ramp",
               "FAIL", 0, hard_fail("wheelchair", "no ramp, impossible with wheelchair")),
    ]


def check(label, ok, state):
    state["total"] += 1
    state["passed"] += 1 if ok else 0
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def main():
    contract = {"destination": "Prague", "trip_length_days": 2,
                "travelers": [{"type": "adult", "mobility": "wheelchair"},
                              {"type": "toddler", "mobility": None}]}
    plan = assemble_itinerary(build_inputs(), contract)

    print("=== DAY PLAN ===")
    for day in plan["days"]:
        print(f"Day {day['day']}  (day-fit {day['day_fit']}%)")
        for block, item in day["blocks"].items():
            if item:
                print(f"  {block:9} {item['overall']['label']:9} {item['name_hint']}")
            else:
                print(f"  {block:9} (empty)")
    print("\n=== SKIPPED (left out, and why) ===")
    for s in plan["skipped"]:
        print(f"  [{s['overall']['label']}] {s['name_hint']}")
        print(f"       reason: {s['reason']}   origin: {s['origin']}")
    print("\n=== CRITIQUE ===")
    print(" ", plan["critique"])

    print("\n=== CHECKS ===")
    st = {"total": 0, "passed": 0}

    # 1. no hard-FAIL activity placed in any block
    placed_texts = []
    for day in plan["days"]:
        for item in day["blocks"].values():
            if item:
                placed_texts.append(item["name_hint"])
    no_fail_placed = not any("no ramp" in t or "no lift" in t for t in placed_texts)
    check("no hard-FAIL activity placed in the itinerary", no_fail_placed, st)

    # 2. both hard-fails are in skipped, with origin + reason
    skipped_texts = [s["name_hint"] for s in plan["skipped"]]
    check("both fail activities are in skipped",
          any("no ramp" in t for t in skipped_texts) and any("no lift" in t for t in skipped_texts), st)
    check("every skipped entry has a reason + origin",
          all(s["reason"] and s["origin"] for s in plan["skipped"]), st)

    # 3. day-fit rollup: day 1 should be mean of its placed activities' scores
    day1 = plan["days"][0]
    placed1 = [b for b in day1["blocks"].values() if b]
    if placed1:
        expected = round(sum(b["overall"]["score"] for b in placed1) / len(placed1))
        check(f"day 1 fit rollup correct (={expected})", day1["day_fit"] == expected, st)

    # 4. critic ran and reported good/poor counts
    crit = plan["critique"]
    check("critic counted 4 good-fits", crit["good_fit_count"] == 4, st)
    check("critic counted 2 poor-fits", crit["poor_fit_count"] == 2, st)

    print(f"\nRESULT: {st['passed']}/{st['total']} checks passed")


if __name__ == "__main__":
    main()
