"""Standalone test for Phase 4a.1 - activity decomposition.

Verifies the fix for the gap Phase 4a exposed: section-sized guide chunks become
individual rateable activities.

Checks:
 1. an activity-rich section ("Things to Do") yields multiple named activities
 2. each activity's description stays grounded in the passage
 3. a pure-logistics section ("Getting Around") yields [] (no invented activities)

Usage:  python3 -m eval.test_decompose   (needs OPENAI_API_KEY, no containers)
"""
from app.agents.decompose import decompose_chunk

CHUNKS = [
    {"name": "activity-rich section", "city": "Prague", "page": 2,
     "text": "## Things to Do\n"
             "Letna Park has flat, paved, wheelchair accessible paths and a "
             "beer garden with skyline views, great for young kids. "
             "Prague Zoo lends strollers and wheelchairs free at the entrance. "
             "A Vltava river cruise is an accessible way to see the city from "
             "the water.",
     "expect_min_activities": 2, "expect_empty": False},

    {"name": "challenging section (famous poor-fits)", "city": "Prague", "page": 8,
     "text": "## Popular but Challenging\n"
             "- Charles Bridge - the iconic must-cross, but its old surface is "
             "bumpy and it's extremely crowded by day. Cross early morning or at "
             "night.\n"
             "- Prague Castle's Old Castle Stairs - a long, steep flight of "
             "steps with no ramp, impossible with a wheelchair.",
     "expect_min_activities": 1, "expect_empty": False},

    {"name": "pure logistics (should yield [])", "city": "Prague", "page": 5,
     "text": "## Getting Around\n"
             "Prague has an excellent, cheap transit network. Tickets are sold "
             "by time and valid across metro, trams, and buses. Low-floor trams "
             "have ramps.",
     "expect_min_activities": 0, "expect_empty": True},
]


def main():
    total = passed = 0
    for ch in CHUNKS:
        print("=" * 70)
        print("CHUNK:", ch["name"])
        acts = decompose_chunk(ch)
        print(f"  -> {len(acts)} activities")
        for a in acts:
            fam = " [FAMOUS]" if a.get("is_famous") else ""
            print(f"     - {a['name']}{fam}")
            print(f"       desc: {a['text']}")
            print(f"       section_hint: {a.get('section_hint')}")

        print("  CHECKS:")
        if ch["expect_empty"]:
            total += 1
            ok = len(acts) == 0
            passed += 1 if ok else 0
            print(f"    [{'PASS' if ok else 'FAIL'}] logistics section yields no activities")
        else:
            total += 1
            ok = len(acts) >= ch["expect_min_activities"]
            passed += 1 if ok else 0
            print(f"    [{'PASS' if ok else 'FAIL'}] >= {ch['expect_min_activities']} activities extracted")
            # grounding: each description should share words with the source passage
            total += 1
            src = ch["text"].lower()
            grounded = all(any(w in src for w in a["text"].lower().split()[:5]) for a in acts) if acts else False
            passed += 1 if grounded else 0
            print(f"    [{'PASS' if grounded else 'FAIL'}] descriptions grounded in passage")
        print()

    print("=" * 70)
    print(f"RESULT: {passed}/{total} checks passed")
    print("Eyeball: real named places (Letna Park, Prague Zoo, Charles Bridge)")
    print("should appear as separate activities; Getting Around should be empty.")


if __name__ == "__main__":
    main()
