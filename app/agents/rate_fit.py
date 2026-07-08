"""Phase 2 - Constraint-fit rating.

Rates ONE activity (a retrieved guide chunk) against the traveler's constraint
contract, producing a per-constraint rating + an overall fit.

TWO LAYERS (this is the whole point):

  Layer 1  CODE (deterministic).  For HARD constraints (wheelchair, budget) we
           check the facts and LOCK the result. The LLM cannot override these.
             - budget: numeric compare (price vs limit) when a price is known.
             - wheelchair: scan the guide text for step-free / no-step evidence.
               NOTE: today this reads PROSE, so the "fact" is a text-extracted
               signal, not a database boolean. When the structured `activities`
               table lands, swap _wheelchair_fact() to read that column and the
               lock becomes truly deterministic. Marked clearly below.

  Layer 2  LLM (constrained).  For SOFT constraints (toddler, senior, pace, and
           dietary-as-preference) the LLM judges, BUT:
             - it must use the fixed label set (no free-form),
             - it must CITE the guide sentence it relied on (no cite -> untrusted),
             - it is TOLD the hard results are already locked (shown to it).

  Combine  CODE has the final say. A required hard FAIL drops overall to FAIL.

Output per constraint: {label, score, basis, citation}
  label:  EXCELLENT | GOOD | TOUGH | FAIL   (for the UI pills)
  score:  0-100 hidden numeric              (for Phase 3 sorting / day-fit %)
  basis:  "data" (code-locked) | "guide" (LLM+citation) | "unknown"
  citation: the guide sentence (soft) or the matched fact (hard), or null
"""
import re
from app import gateway
from app.observability import observe
from app.stores import bank as bank_store

# Fixed label set + their representative numeric scores (label -> score midpoint).
_LABEL_SCORE = {"EXCELLENT": 90, "GOOD": 70, "TOUGH": 35, "FAIL": 0, "UNKNOWN": 35}
_VALID_LABELS = set(_LABEL_SCORE)

# Phrases that signal step-free / accessible vs. explicitly NOT.
_STEP_FREE_POS = [
    "wheelchair accessible", "wheelchair-accessible", "step-free", "step free",
    "ramp", "ramped", "lift", "elevator", "accessible entrance", "barrier-free",
    "barrier free", "level access", "flat", "paved",
]
_STEP_FREE_NEG = [
    "stairs", "steps", "not wheelchair", "no wheelchair", "not accessible",
    "inaccessible", "cobblestone", "cobbles", "steep", "uneven", "no lift",
    "no elevator", "no ramp",
]


# ---------- Layer 1: deterministic hard-fact checks ----------

def _wheelchair_fact(text: str):
    """Extract a step-free signal from guide prose.

    Returns (label, score, citation) or None if no signal found.
    *** When the structured activities table exists, replace this body with a
    direct boolean read: wheelchair_accessible True->EXCELLENT, False->FAIL. ***
    """
    t = text.lower()

    # 1) Negated-positive guard: "no ramp", "no lift", "without elevator" etc.
    #    contain a positive word but are actually NEGATIVE. Strip these spans out
    #    before positive matching so a negated positive can't count as a pass.
    neg_positive = [
        "no ramp", "no lift", "no elevator", "not wheelchair accessible",
        "not accessible", "without ramp", "without a ramp", "without lift",
        "without an elevator", "no step-free", "no step free", "not step-free",
    ]
    hard_negatives_found = [p for p in neg_positive if p in t]
    # remove those spans so their inner positive word ("ramp"/"lift") isn't recounted
    t_scrub = t
    for p in hard_negatives_found:
        t_scrub = t_scrub.replace(p, " ")

    neg = [p for p in _STEP_FREE_NEG if p in t_scrub] + hard_negatives_found
    pos = [p for p in _STEP_FREE_POS if p in t_scrub]

    # 2) An explicit "impossible with a wheelchair / no ramp / not accessible"
    #    statement is a hard FAIL regardless of any leftover positive words.
    hard_fail_markers = hard_negatives_found + [
        m for m in ("impossible with a wheelchair", "cannot be accessed",
                    "wheelchair users cannot", "not possible for wheelchair")
        if m in t
    ]
    if hard_fail_markers:
        return ("FAIL", 0, f"guide text states: {hard_fail_markers[0]}")

    # 3) Otherwise weigh remaining signals.
    if neg and not pos:
        return ("FAIL", 0, f"guide text mentions: {', '.join(neg[:3])}")
    if neg and pos:
        # genuine mixed signals (e.g. "ramp to courtyard but temple has stairs")
        return ("TOUGH", 35, f"mixed access signals: +[{', '.join(pos[:2])}] -[{', '.join(neg[:2])}]")
    if pos:
        return ("EXCELLENT", 90, f"guide text mentions: {', '.join(pos[:3])}")
    return None  # no signal -> unknown, let it be flagged rather than guessed


def _budget_fact(text: str, budget: dict):
    """Compare any price found in the text to the budget limit.

    Returns (label, score, citation) or None if no price/limit to compare.
    """
    if not budget or budget.get("amount") is None:
        return None
    limit = budget["amount"]
    # find $-amounts or bare numbers near a currency word
    prices = [float(x) for x in re.findall(r"\$\s?(\d+(?:\.\d+)?)", text)]
    if not prices:
        return None
    lo = min(prices)
    if lo <= limit:
        return ("GOOD", 70, f"price ~${lo:.0f} within ${limit:.0f} limit")
    return ("FAIL", 0, f"price ~${lo:.0f} exceeds ${limit:.0f} limit")


def _hard_layer(text: str, contract: dict) -> dict:
    """Run all deterministic hard checks. Returns {constraint: rating}."""
    out = {}
    trav = contract.get("travelers") or []
    needs_wheelchair = any((p or {}).get("mobility") == "wheelchair" for p in trav)
    if needs_wheelchair:
        fact = _wheelchair_fact(text)
        if fact:
            label, score, cite = fact
            out["wheelchair"] = {"label": label, "score": score,
                                 "basis": "data", "citation": cite, "hard": True}
        else:
            out["wheelchair"] = {"label": "UNKNOWN", "score": 35, "basis": "unknown",
                                 "citation": "no step-free info in the guide for this place", "hard": True}
    b = _budget_fact(text, contract.get("budget"))
    if b:
        label, score, cite = b
        out["budget"] = {"label": label, "score": score,
                         "basis": "data", "citation": cite, "hard": True}
    return out


# ---------- Layer 2: constrained LLM soft judgment ----------

_SOFT_PROMPT = """You rate how well ONE activity fits a traveler's SOFT needs.
Use ONLY the guide text provided. Return ONLY JSON.

Rules:
- For each soft need, choose EXACTLY one label: EXCELLENT, GOOD, TOUGH, FAIL.
- The "citation" must explain the rating IN TERMS OF THAT SPECIFIC NEED, grounded
  in the guide text. Translate the guide's facts into what matters for the need:
  * toddler-friendly / stroller: talk about stairs, steep or unpaved paths, uneven
    ground, narrow crowded lanes, stroller access - NOT wheelchair wording.
  * senior-friendly: talk about long walking, stairs, steep climbs, seating, pace.
  Do NOT quote a wheelchair-specific sentence as the reason for a toddler or senior
  need; describe the underlying obstacle (e.g. "steep stairs and unpaved paths make
  it hard with a stroller") using the guide's facts.
- If the guide text says nothing relevant to a need, use label "TOUGH" and
  citation "no relevant info".
- These HARD results are already LOCKED by data and you must NOT contradict or
  re-rate them: {locked}
- Do not invent facts not in the guide text.

Soft needs to rate: {needs}

Guide text:
\"\"\"{text}\"\"\"

Return ONLY JSON: {{"ratings": {{<need>: {{"label": <LABEL>, "citation": <guide sentence>}}}}}}"""


def _soft_needs(contract: dict) -> list[str]:
    needs = []
    trav = contract.get("travelers") or []
    if any((p or {}).get("type") == "toddler" for p in trav):
        needs.append("toddler-friendly")
    if any((p or {}).get("type") == "senior" for p in trav):
        needs.append("senior-friendly")
    if any((p or {}).get("mobility") == "stroller" for p in trav):
        needs.append("stroller-friendly")
    # dietary preferences (non-medical) are soft; medical handled as hard elsewhere later
    for d in (contract.get("dietary") or []):
        if not d.get("medical"):
            needs.append(f"dietary: {d.get('need')}")
    for o in (contract.get("other") or []):
        needs.append(o)
    return needs


def _soft_layer(text: str, contract: dict, locked: dict, user_id=None) -> dict:
    needs = _soft_needs(contract)
    if not needs:
        return {}
    locked_str = ", ".join(f"{k}={v['label']}" for k, v in locked.items()) or "(none)"
    try:
        out = gateway.chat_json(
            "clarify",
            [{"role": "user", "content": _SOFT_PROMPT.format(
                locked=locked_str, needs="; ".join(needs), text=text[:1500])}],
            user_id=user_id,
        )
        raw = out.get("ratings", {}) or {}
    except Exception:
        raw = {}
    result = {}
    for need in needs:
        r = raw.get(need) or {}
        label = str(r.get("label", "TOUGH")).upper()
        if label not in _VALID_LABELS:
            label = "TOUGH"
        cite = r.get("citation") or "no relevant info"
        # a soft rating with no real citation is downgraded (can't be trusted high)
        trusted = cite and cite != "no relevant info"
        if not trusted and label in ("EXCELLENT", "GOOD"):
            label = "TOUGH"
        result[need] = {"label": label, "score": _LABEL_SCORE[label],
                        "basis": "guide", "citation": cite, "hard": False}
    return result


# ---------- Combine ----------

# ---------- Bank path: use precomputed, confidence-scored facts ----------

# How confidence affects the hard-lock:
#   HIGH   -> a FAIL is a true hard wall (locked, LLM/blend cannot lift it)
#   MEDIUM -> FAIL still blocks, but flagged as firm-not-absolute
#   LOW    -> treated as "likely, verify": a FAIL becomes TOUGH (soft), not a wall
_CONF_HARD = {"HIGH": True, "MEDIUM": True, "LOW": False}


def _bank_constraint(label: str, confidence: str, kind: str) -> dict:
    """Turn a bank cell into a per-constraint rating with confidence-aware locking."""
    label = str(label or "UNKNOWN").upper()
    if label not in _VALID_LABELS:
        label = "UNKNOWN"
    conf = str(confidence or "LOW").upper()
    is_hard_kind = kind in ("wheelchair", "budget")
    lock = is_hard_kind and _CONF_HARD.get(conf, False)

    # LOW-confidence FAIL/UNKNOWN shouldn't hard-block; soften to TOUGH.
    if is_hard_kind and not lock and label in ("FAIL", "UNKNOWN"):
        label = "TOUGH"

    return {"label": label, "score": _LABEL_SCORE[label],
            "basis": f"bank({conf})", "citation": None, "hard": lock,
            "confidence": conf}


def _rate_from_bank(activity: dict, contract: dict, user_id=None):
    """Try to rate this activity from the bank. Returns per_constraint dict or None."""
    name = activity.get("name") or ""
    city = activity.get("city") or contract.get("destination") or ""
    if not name:
        return None
    row, how = bank_store.resolve(name, city, user_id=user_id)
    if not row:
        return None

    conf = row.get("confidence", "LOW")
    note = (row.get("note") or "").strip()
    per = {}
    trav = contract.get("travelers") or []

    if any((t or {}).get("mobility") == "wheelchair" for t in trav):
        c = _bank_constraint(row.get("wheelchair"), conf, "wheelchair")
        c["citation"] = note or row.get("source")
        per["wheelchair"] = c
    if any((t or {}).get("type") == "toddler" for t in trav) or \
       any((t or {}).get("mobility") == "stroller" for t in trav):
        c = _bank_constraint(row.get("toddler"), conf, "toddler")
        c["citation"] = note or row.get("source")
        per["toddler-friendly"] = c
    if any((t or {}).get("type") == "senior" for t in trav):
        c = _bank_constraint(row.get("senior"), conf, "senior")
        c["citation"] = note or row.get("source")
        per["senior-friendly"] = c

    return per or None


def rate_activity(activity: dict, contract: dict, user_id=None) -> dict:
    """Rate one activity (retrieved chunk) against the contract.

    activity: {"text"/"quote": str, "city": str, "page": ...}
    Returns {overall: {label,score}, per_constraint: {...}, activity: {...}}
    """
    text = activity.get("text") or activity.get("quote") or ""

    # Prefer the precomputed bank (confidence-scored, lockable). Fall back to
    # deriving from guide prose only when the place isn't in the bank.
    per = _rate_from_bank(activity, contract, user_id=user_id)
    used_bank = per is not None
    if per is None:
        hard = _hard_layer(text, contract)
        soft = _soft_layer(text, contract, hard, user_id=user_id)
        per = {**hard, **soft}

    # Overall: any REQUIRED hard FAIL drops overall to FAIL. Else weighted-ish min/mean.
    hard_fail = any(v["hard"] and v["label"] == "FAIL" for v in per.values())
    if hard_fail:
        overall = {"label": "FAIL", "score": 0}
    elif per:
        # overall leans toward the weakest constraint (a plan is only as good as its worst fit)
        min_score = min(v["score"] for v in per.values())
        mean_score = sum(v["score"] for v in per.values()) / len(per)
        blended = round(0.6 * min_score + 0.4 * mean_score)
        label = ("EXCELLENT" if blended >= 80 else "GOOD" if blended >= 55
                 else "TOUGH" if blended >= 20 else "FAIL")
        overall = {"label": label, "score": blended}
    else:
        # No constraints to judge: nothing disqualifies this place, so a valid
        # famous attraction is a GOOD default fit (not TOUGH). Without this, an
        # unconstrained trip scores everything below the good-fit threshold and
        # places nothing — an empty plan.
        overall = {"label": "GOOD", "score": 70}

    ret_activity = {"name": activity.get("name"), "city": activity.get("city"),
                    "page": activity.get("page"), "text": text[:200]}
    if activity.get("lat") is not None and activity.get("lng") is not None:
        ret_activity["lat"], ret_activity["lng"] = activity["lat"], activity["lng"]
    if activity.get("image_url"):
        ret_activity["image_url"] = activity["image_url"]

    return {
        "activity": ret_activity,
        "overall": overall,
        "per_constraint": per,
        "used_bank": used_bank,
    }
