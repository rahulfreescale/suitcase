"""Phase 4a demo - run the REAL constraint-planning pipeline end-to-end.

Unlike the isolated phase tests (hand-built inputs), this hits the LIVE stack:
real retrieval over the 26-city index, real rating, real assembly. It proves the
whole chain produces a sensible plan from an actual request.

Usage (needs containers up + OPENAI_API_KEY, like the app):
    make up            # opensearch/redis/postgres must be running
    python3 -m eval.demo_plan_pipeline
    python3 -m eval.demo_plan_pipeline "5 days in Lisbon with a stroller, veggie food"

Read the output critically:
 - did it extract the right contract?
 - did REAL Prague activities get retrieved and rated?
 - did good-fits land in day blocks and famous poor-fits go to skipped?
 - does the day-fit rollup look right, and did the critic flag weak days?
"""
import sys
from app.agents.plan_pipeline import plan_trip


def _print_plan(result):
    print("\n" + "=" * 72)
    print("CONTRACT")
    print("=" * 72)
    c = result["contract"]
    print(f"  destination : {c.get('destination')}")
    print(f"  trip length : {c.get('trip_length_days')} days")
    print(f"  travelers   : {c.get('travelers')}")
    print(f"  budget      : {c.get('budget')}")
    print(f"  dietary     : {c.get('dietary')}")
    print(f"  CHIPS detected : {result['chips']['detected']}")
    print(f"  CHIPS suggested: {result['chips']['suggested']}")

    if result.get("needs_clarification"):
        print("\n  >> NEEDS CLARIFICATION:", result["clarification_question"])
        return

    it = result["itinerary"]
    print("\n" + "=" * 72)
    print("RATED DAY PLAN")
    print("=" * 72)
    for day in it["days"]:
        print(f"\nDay {day['day']}   (day-fit {day['day_fit']}%)")
        for block, item in day["blocks"].items():
            if item:
                pills = " ".join(f"{k}={v['label']}" for k, v in item["per_constraint"].items())
                print(f"  {block:9} [{item['overall']['label']:9}] {item['name_hint']}")
                print(f"            {pills}")
            else:
                print(f"  {block:9} (open)")

    print("\n" + "=" * 72)
    print("SKIPPED - popular spots left out, and why")
    print("=" * 72)
    if not it["skipped"]:
        print("  (none - every retrieved activity fit well enough)")
    for s in it["skipped"]:
        print(f"  [{s['overall']['label']:5}] {s['name_hint']}")
        print(f"          why: {s['reason']}")
        print(f"          origin: {s['origin']}")

    print("\n" + "=" * 72)
    print("CRITIQUE (generator-critic loop signal)")
    print("=" * 72)
    crit = it["critique"]
    print(f"  needs_replan : {crit['needs_replan']}")
    print(f"  good-fits    : {crit['good_fit_count']}   poor-fits: {crit['poor_fit_count']}")
    for wd in crit["weak_days"]:
        print(f"  weak day {wd['day']}: {wd['issue']} (fit {wd['day_fit']}%, {wd['placed']} placed)")


def main():
    request = (sys.argv[1] if len(sys.argv) > 1
               else "plan a 2 day Prague trip with a toddler and someone in a wheelchair")
    print("REQUEST:", request)
    result = plan_trip(request)
    _print_plan(result)
    print("\n(Chain ran: extract -> real retrieval -> rate -> assemble.)")


if __name__ == "__main__":
    main()
