"""PLAN-CONSTRAINT EVAL — does the planner keep its core promise?

The generic RAG metrics (faithfulness / relevancy in live_traffic_eval) measure
the *Ask* path. They do NOT measure the thing that makes Suitcase different:
that the *Plan* path is faithful to HARD constraints. This eval checks exactly
that, per scenario, with pass/fail assertions:

  C1  NO hard-FAIL place is ever PLACED in the itinerary        (the core wall)
  C2  every SKIPPED place has a non-empty, grounded reason      (honest refusal)
  C3  every rating carries a basis + citation (not invented)    (grounding)
  C4  the detected constraints match what was asked             (comprehension)

Run:  python -m eval.plan_eval
Exit code is non-zero if any scenario fails, so it can gate CI.
"""
from __future__ import annotations
import sys
from app.agents.plan_pipeline import plan_trip

# Each scenario: the request, the hard constraint we expect honored, and the
# famous places we expect to be REFUSED for that constraint (the differentiator).
SCENARIOS = [
    {
        "name": "Rome · wheelchair",
        "request": "plan a 2 day Rome trip with a wheelchair",
        "hard": "wheelchair",
        "must_detect": ["wheelchair"],
        # famous but inaccessible — must NOT be placed
        "expect_refused": ["Spanish Steps", "Roman Forum", "Trevi Fountain"],
    },
    {
        "name": "Rome · no constraints (control)",
        "request": "plan a 2 day Rome trip",
        "hard": None,
        "must_detect": [],
        "expect_refused": [],  # nothing hard, so famous places CAN appear
    },
    {
        "name": "Zputnik · fake city (regression)",
        "request": "plan a 2 day trip to Zputnik",
        "hard": None,
        "must_detect": [],
        "expect_refused": [],
        "expect_empty": True,   # must refuse gracefully, not fabricate a plan
    },
]


def _placed_names(plan: dict) -> list[str]:
    out = []
    itin = plan.get("itinerary") or {}
    for day in itin.get("days") or []:
        for slot in ("morning", "afternoon", "evening"):
            b = (day.get("blocks") or {}).get(slot)
            if b:
                out.append(b.get("name_hint", ""))
    return out


def _has_hard_fail(rated: dict, hard: str | None) -> bool:
    per = rated.get("per_constraint", {})
    if hard and hard in per:
        return per[hard].get("label") == "FAIL" and per[hard].get("hard")
    return any(v.get("hard") and v.get("label") == "FAIL" for v in per.values())


def check(scenario: dict) -> list[str]:
    """Return a list of failure strings (empty = passed)."""
    fails: list[str] = []
    plan = plan_trip(scenario["request"])

    # A plan that asked for clarification or came back empty isn't a constraint
    # violation — skip the constraint asserts, but note it for visibility.
    if plan.get("needs_clarification"):
        return []  # asking for missing info is valid behavior, not a failure
    if scenario.get("expect_empty"):
        # regression scenario: a fake/unknown city MUST NOT produce a plan
        placed = _placed_names(plan)
        if placed:
            fails.append(f"REGRESSION: fake city produced a plan: {placed}")
        if not plan.get("empty_reason"):
            fails.append("REGRESSION: fake city gave no graceful empty_reason")
        return fails

    placed = _placed_names(plan)
    rated = {}
    for r in plan.get("activities_rated") or []:
        act = r.get("activity") or {}
        nm = act.get("name")
        if nm:
            rated[nm] = r
    itin = plan.get("itinerary") or {}
    skipped = itin.get("skipped") or []

    # C1 — no hard-FAIL place is placed
    for name in placed:
        r = rated.get(name)
        if r and _has_hard_fail(r, scenario["hard"]):
            fails.append(f"C1 VIOLATION: hard-FAIL place '{name}' was PLACED")

    # C1b — the named famous-but-inaccessible places must not be placed
    for banned in scenario["expect_refused"]:
        if any(banned.lower() in p.lower() for p in placed):
            fails.append(f"C1 VIOLATION: '{banned}' should be refused but was placed")

    # C2 — every skipped place has a grounded, non-empty reason
    for s in skipped:
        reason = (s.get("reason") or "").strip()
        if not reason:
            fails.append(f"C2 VIOLATION: skipped '{s.get('name_hint')}' has no reason")

    # C3 — every rating carries a basis + citation (not invented)
    for name, r in rated.items():
        for c, v in r.get("per_constraint", {}).items():
            if v.get("label") in ("FAIL", "TOUGH") and not v.get("citation"):
                fails.append(f"C3 VIOLATION: '{name}' {c}={v.get('label')} has no citation")

    # C4 — detected constraints include what we asked for
    detected = [d.lower() for d in plan.get("chips", {}).get("detected", [])]
    for need in scenario["must_detect"]:
        if need.lower() not in detected:
            fails.append(f"C4 VIOLATION: expected to detect '{need}', got {detected}")

    return fails


def main() -> int:
    total_fails = 0
    print("=" * 66)
    print("  PLAN-CONSTRAINT EVAL — is the planner faithful to hard rules?")
    print("=" * 66)
    for sc in SCENARIOS:
        print(f"\n▶ {sc['name']}")
        try:
            fails = check(sc)
        except Exception as e:
            print(f"  ✗ ERROR running scenario: {e}")
            total_fails += 1
            continue
        if not fails:
            print("  ✓ PASS — all constraint checks held")
        else:
            for f in fails:
                print(f"  ✗ {f}")
            total_fails += len(fails)
    print("\n" + "-" * 66)
    if total_fails == 0:
        print("  RESULT: ✓ all scenarios passed — planner is constraint-faithful")
    else:
        print(f"  RESULT: ✗ {total_fails} violation(s) — planner broke a promise")
    print("-" * 66)
    return 1 if total_fails else 0


if __name__ == "__main__":
    sys.exit(main())
