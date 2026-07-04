"""Standalone test for the Phase 2 constraint-fit rater.

Proves the two properties that make the rating trustworthy:
  1. HARD-LOCK: if the guide text says an activity has stairs / no step-free
     route, a wheelchair traveler gets a locked FAIL that the LLM cannot lift.
  2. CITATIONS: every SOFT rating carries a guide citation (or is downgraded).

Usage:  python3 -m eval.test_rate_fit
Needs OPENAI_API_KEY. No OpenSearch/Redis (uses hand-written activity chunks).
"""
import json
from app.agents.rate_fit import rate_activity

# Hand-written "activities" (mimicking retrieved guide chunks) with known truth,
# so we can assert the rater behaves. Text is representative of the real corpus.
CASES = [
    {
        "name": "Old Castle Stairs vs wheelchair -> must LOCK FAIL",
        "activity": {"city": "Prague", "page": 1,
            "text": "The Old Castle Stairs are a long, steep flight of steps "
                    "with no ramp, impossible with a wheelchair or stroller."},
        "contract": {"destination": "Prague",
            "travelers": [{"type": "adult", "mobility": "wheelchair"},
                          {"type": "toddler", "mobility": None}]},
        "expect": {"wheelchair_label": "FAIL", "overall_label": "FAIL",
                   "wheelchair_basis": "data"},
    },
    {
        "name": "Step-free park -> wheelchair EXCELLENT, toddler cited",
        "activity": {"city": "Prague", "page": 2,
            "text": "Letna Park has flat, paved, wheelchair accessible paths and "
                    "a relaxed beer garden; stroller-friendly and great for young kids."},
        "contract": {"destination": "Prague",
            "travelers": [{"type": "adult", "mobility": "wheelchair"},
                          {"type": "toddler", "mobility": None}]},
        "expect": {"wheelchair_label": "EXCELLENT", "toddler_has_citation": True},
    },
    {
        "name": "Over-budget activity -> budget LOCK FAIL",
        "activity": {"city": "Queenstown", "page": 3,
            "text": "The tandem paragliding flight costs $279 per person and "
                    "launches from the mountaintop."},
        "contract": {"destination": "Queenstown",
            "travelers": [{"type": "adult", "mobility": None}],
            "budget": {"amount": 150, "unit": "per_day"}},
        "expect": {"budget_label": "FAIL", "budget_basis": "data"},
    },
    {
        "name": "Soft-only (toddler) with clear guide support -> cited GOOD/EXCELLENT",
        "activity": {"city": "Tokyo", "page": 4,
            "text": "Ueno Park is huge, flat, and stroller-perfect, with a zoo "
                    "and wide open paths popular with families and small children."},
        "contract": {"destination": "Tokyo",
            "travelers": [{"type": "toddler", "mobility": "stroller"}]},
        "expect": {"toddler_has_citation": True, "no_hard_fail": True},
    },
]


def _get(per, *keys):
    """Find a per-constraint entry by any of the candidate keys (labels vary)."""
    for k in keys:
        if k in per:
            return per[k]
    # fuzzy: match on prefix (e.g. "toddler-friendly")
    for pk, pv in per.items():
        if any(pk.startswith(k) or k in pk for k in keys):
            return pv
    return None


def main():
    total = passed = 0
    for case in CASES:
        print("=" * 70)
        print("CASE:", case["name"])
        print("ACTIVITY:", case["activity"]["text"])
        res = rate_activity(case["activity"], case["contract"])
        per = res["per_constraint"]
        print("\nOVERALL:", res["overall"])
        print("PER-CONSTRAINT:")
        for k, v in per.items():
            print(f"  {k}: {v['label']} (score {v['score']}, basis={v['basis']})")
            print(f"      citation: {v['citation']}")

        exp = case["expect"]
        print("\nCHECKS:")
        def check(label, ok):
            nonlocal total, passed
            total += 1; passed += 1 if ok else 0
            print(f"  [{'PASS' if ok else 'FAIL'}] {label}")

        if "wheelchair_label" in exp:
            wc = _get(per, "wheelchair")
            check(f"wheelchair == {exp['wheelchair_label']}",
                  wc and wc["label"] == exp["wheelchair_label"])
        if "wheelchair_basis" in exp:
            wc = _get(per, "wheelchair")
            check(f"wheelchair basis == {exp['wheelchair_basis']}",
                  wc and wc["basis"] == exp["wheelchair_basis"])
        if "budget_label" in exp:
            b = _get(per, "budget")
            check(f"budget == {exp['budget_label']}", b and b["label"] == exp["budget_label"])
        if "budget_basis" in exp:
            b = _get(per, "budget")
            check(f"budget basis == {exp['budget_basis']}", b and b["basis"] == exp["budget_basis"])
        if "overall_label" in exp:
            check(f"overall == {exp['overall_label']}",
                  res["overall"]["label"] == exp["overall_label"])
        if exp.get("toddler_has_citation"):
            t = _get(per, "toddler-friendly", "toddler")
            ok = t and t["citation"] and t["citation"] != "no relevant info"
            check("toddler rating has a real citation", ok)
        if exp.get("no_hard_fail"):
            check("no hard FAIL present",
                  not any(v["hard"] and v["label"] == "FAIL" for v in per.values()))

        print()

    print("=" * 70)
    print(f"RESULT: {passed}/{total} checks passed")
    print("Key things to eyeball above:")
    print(" - the stairs activity LOCKED wheelchair=FAIL (basis=data), LLM couldn't lift it")
    print(" - the over-budget activity LOCKED budget=FAIL (basis=data)")
    print(" - every soft (toddler/stroller) rating shows a guide citation")


if __name__ == "__main__":
    main()
