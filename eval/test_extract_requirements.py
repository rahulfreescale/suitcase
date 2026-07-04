"""Standalone test for the Extract Requirements agent.

Run BEFORE wiring the extractor into the graph, to verify:
  request  ->  correct typed constraint contract  +  detected/suggested chips.

Usage:
    python3 -m eval.test_extract_requirements

Needs: OPENAI_API_KEY (uses the same gateway/LLM as the app). No OpenSearch/
Redis needed - this tests extraction in isolation.
"""
import json
from app.agents.extract_requirements import extract_requirements

# Each case: the request, plus a few soft assertions describing what a correct
# extraction should contain. Assertions are intentionally lenient (the LLM may
# phrase labels differently) - they check STRUCTURE, not exact wording.
CASES = [
    {
        "name": "Prague: toddler + wheelchair (the demo query)",
        "query": "plan Prague with a toddler and someone in a wheelchair",
        "expect": {
            "destination": "Prague",
            "has_wheelchair": True,
            "has_toddler": True,
            "trip_len_missing": True,   # no days stated -> should ask
        },
    },
    {
        "name": "Lisbon: budget + veggie preference + 4 days",
        "query": "4 days in Lisbon, we're vegetarian, keep it under $150 a night",
        "expect": {
            "destination": "Lisbon",
            "trip_len": 4,
            "has_budget": True,
            "dietary_medical": False,   # "vegetarian" = preference, not medical
        },
    },
    {
        "name": "Tokyo: medical dietary (nut allergy) should flag medical=true",
        "query": "5 days in Tokyo with my kids, one has a severe nut allergy",
        "expect": {
            "destination": "Tokyo",
            "trip_len": 5,
            "dietary_medical": True,    # severe allergy -> medical
        },
    },
    {
        "name": "Missing destination -> needs clarification",
        "query": "somewhere relaxing for a week with my elderly mother who uses a wheelchair",
        "expect": {
            "destination_missing": True,
            "has_wheelchair": True,
            "has_senior": True,
        },
    },
]


def _contract_checks(contract, detected, expect):
    """Return list of (label, passed) for the given expectations."""
    checks = []
    trav = contract.get("travelers") or []
    diet = contract.get("dietary") or []

    if "destination" in expect:
        checks.append((f"destination == {expect['destination']}",
                       (contract.get("destination") or "").lower() == expect["destination"].lower()))
    if expect.get("destination_missing"):
        checks.append(("destination is missing", not contract.get("destination")))
    if "trip_len" in expect:
        checks.append((f"trip_length_days == {expect['trip_len']}",
                       contract.get("trip_length_days") == expect["trip_len"]))
    if expect.get("trip_len_missing"):
        checks.append(("trip_length_days missing", contract.get("trip_length_days") is None))
    if expect.get("has_wheelchair"):
        checks.append(("wheelchair detected", "wheelchair" in detected))
    if expect.get("has_toddler"):
        checks.append(("toddler detected", "toddler" in detected))
    if expect.get("has_senior"):
        checks.append(("senior detected", "senior" in detected))
    if expect.get("has_budget"):
        checks.append(("budget detected", "budget" in detected))
    if "dietary_medical" in expect:
        got = any(bool(d.get("medical")) for d in diet)
        checks.append((f"dietary medical == {expect['dietary_medical']}",
                       got == expect["dietary_medical"]))
    return checks


def main():
    total = 0
    passed = 0
    for case in CASES:
        print("=" * 70)
        print("CASE:", case["name"])
        print("REQUEST:", case["query"])
        state = {"query": case["query"], "thread_id": "test"}
        result = extract_requirements(state)
        contract = result["constraints"]
        detected = result["detected_constraints"]
        suggested = result["suggested_constraints"]

        print("\nDETECTED (green chips):", detected or "(none)")
        print("SUGGESTED (+ add chips):", suggested)
        if result.get("needs_clarification"):
            print("NEEDS CLARIFICATION ->", result.get("clarification_question"))
        print("\nCONTRACT:")
        print(json.dumps(contract, indent=2, ensure_ascii=False))

        print("\nCHECKS:")
        for label, ok in _contract_checks(contract, detected, case["expect"]):
            total += 1
            passed += 1 if ok else 0
            print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        print()

    print("=" * 70)
    print(f"RESULT: {passed}/{total} checks passed")
    print("(Read the CONTRACT blocks above - the point is that extraction is")
    print(" correct and complete, not just that assertions pass.)")


if __name__ == "__main__":
    main()
