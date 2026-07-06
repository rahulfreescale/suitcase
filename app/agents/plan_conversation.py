"""Planning Conversation Agent — turns one-shot planning into a dialogue.

Instead of demanding a fully-specified request up front, this agent holds a
conversation: it accumulates a trip contract across turns, and at each turn
DECIDES whether it has enough to plan a good trip or whether one more targeted
question would materially improve the result. That ask-vs-plan judgment is the
agentic part — it isn't a fixed form; what's worth asking depends on what the
user has already said and what we already know about them from past visits.

It leans on the existing memory system:
  - short-term: the conversation so far (turns)
  - long-term:  durable user facts (e.g. "usually travels vegetarian"), so a
                returning user is asked fewer questions.

Output per turn is one of:
  {"action": "ask",  "message": <one friendly question>, "contract": {...}}
  {"action": "plan", "message": <"building..." line>,     "contract": {...}}

The caller runs `plan`/agents only when action == "plan".
"""
from __future__ import annotations
import json
from app import gateway
from app.stores import memory as mem

# Hard cap: never ask more than this many questions before committing to a plan.
_MAX_QUESTIONS = 5


_SYS = """You are the planning host for an accessibility-first travel concierge.
You are having a short, friendly conversation to gather what you need before
building a trip plan. You are warm and efficient — never robotic, never a form.

You maintain a CONTRACT (what you know about the trip). Each turn you either ask
ONE more question or decide you have enough and plan.

REQUIRED to plan at all: destination AND number of days. If either is missing,
you MUST ask for it.

WORTH asking about (only if not already known and genuinely useful):
- who's traveling + access needs (wheelchair, toddler/stroller, senior)
- travel month or season (affects weather-based advice)
- strong climate feelings ("I hate cold")
- dietary needs
- budget level
- pace / interests

CAPTURE special_needs when the user states them, then MOVE ON — do NOT keep
re-asking about the same need once you've noted it. medical (chronic condition,
needs pharmacies/hospitals), sensory (autism, anxiety in crowds, needs quiet
spaces), heat_sensitive (overheats, tires easily, low stamina, heart/lung
condition, pregnancy). Only set true when actually stated. IMPORTANT: if the
user has already told you about a need (e.g. "I need pharmacy access"), set the
flag and do NOT ask about it again — asking twice about the same thing is
annoying. The research agent will handle the specifics; you just capture the flag.

RULES for deciding ask vs plan:
- Ask ONE question at a time. Make it warm and specific, and it's fine to ask
  about two closely-related things in one sentence (e.g. "any dietary needs or a
  budget I should keep in mind?").
- DON'T ask about things already in the contract or already known from memory.
- DON'T interrogate. Once you have destination + days + the traveler's access
  needs, and a reasonable sense of the trip, PLAN — don't keep asking for
  nice-to-haves. Most trips should plan after 1-3 questions.
- If the user says "just plan it" / "surprise me" / "no preferences", PLAN NOW
  with what you have.
- You have asked {asked} question(s) so far. If that is {maxq} or more, you MUST
  plan this turn regardless.

Return ONLY JSON:
{{
  "action": "ask" | "plan",
  "message": "<if ask: the single friendly question. if plan: a short warm
              'building your trip now' line that reflects what you gathered>",
  "contract": {{
     "destination": <city or null>,
     "trip_length_days": <int or null>,
     "travelers": [{{"type":"adult|toddler|senior|child","mobility":"wheelchair|stroller|null"}}],
     "dietary": [<strings>],
     "budget": <"budget|mid-range|luxury" or null>,
     "preferences": {{"travel_month": <str|null>, "climate": <str|null>,
                      "interests": [<strings>]}},
     "special_needs": {{"medical": <bool>, "sensory": <bool>, "heat_sensitive": <bool>}}
  }}
}}
The contract must ACCUMULATE — carry forward everything already gathered and add
what's new this turn. Never drop a field you already had."""


def _fmt_history(turns: list[dict]) -> str:
    if not turns:
        return "(no prior turns)"
    out = []
    for t in turns[-8:]:
        if t.get("question"):
            out.append(f"User: {t['question']}")
        if t.get("answer"):
            out.append(f"Host: {t['answer']}")
    return "\n".join(out)


def converse(user_msg: str, contract_so_far: dict | None,
             session_id: str | None = None, user_id: str | None = None,
             asked: int = 0) -> dict:
    """One conversational turn. Returns {action, message, contract, asked}.

    - user_msg: what the user just said
    - contract_so_far: the accumulated contract (None on first turn)
    - asked: how many questions we've already asked this planning session
    """
    session = mem.load_session(session_id)
    user_facts = mem.load_user_memory(user_id)
    history = _fmt_history(session.get("turns", []))
    known = json.dumps(contract_so_far or {}, indent=1)
    facts = "\n".join(f"- {f}" for f in user_facts) if user_facts else "(none yet)"

    sys = _SYS.format(asked=asked, maxq=_MAX_QUESTIONS)
    usr = (
        f"CONVERSATION SO FAR:\n{history}\n\n"
        f"CONTRACT GATHERED SO FAR:\n{known}\n\n"
        f"WHAT WE REMEMBER ABOUT THIS USER (long-term, use to SKIP questions):\n{facts}\n\n"
        f"The user just said: \"{user_msg}\"\n\n"
        f"Update the contract with anything new, then decide: ask one more "
        f"question, or plan now? Return the JSON."
    )

    try:
        out = gateway.chat_json(
            "clarify",
            [{"role": "system", "content": sys}, {"role": "user", "content": usr}],
            user_id=user_id)
    except Exception:
        # on any failure, fall back to planning with what we have (never hang)
        return {"action": "plan",
                "message": "Let me put together a plan with what I have.",
                "contract": contract_so_far or {"destination": None,
                                                "trip_length_days": None},
                "asked": asked}

    action = out.get("action")
    contract = out.get("contract") or contract_so_far or {}
    message = out.get("message") or ""

    # Guard: enforce the hard cap and the required-fields rule deterministically,
    # rather than fully trusting the LLM's self-control.
    have_required = bool(contract.get("destination")) and bool(contract.get("trip_length_days"))

    # Guard against the medical-loop: if we've captured a special need AND have
    # the essentials AND already asked at least twice, stop probing and plan.
    sn = contract.get("special_needs") or {}
    has_special = bool(sn.get("medical") or sn.get("sensory") or sn.get("heat_sensitive"))
    if action == "ask" and have_required and has_special and asked >= 2:
        action = "plan"
        message = "Perfect — I've got your trip details and your needs noted. Building it now."

    if action == "ask":
        if asked >= _MAX_QUESTIONS and have_required:
            action = "plan"
            message = "Great — I have enough to build you a solid plan. Working on it now."
        else:
            asked += 1
    if action == "plan" and not have_required:
        # model tried to plan without the essentials — force one more ask
        action = "ask"
        missing = "which city" if not contract.get("destination") else "how many days"
        if not message or "?" not in message:
            message = f"Happy to plan that — just tell me {missing}?"
        asked += 1

    # Product invariant: this is an accessibility-first planner, so it must never
    # plan a FIRST bare request without at least asking about who's travelling /
    # access needs. The model's ask-vs-plan judgment varies by city (Venice→plan,
    # Paris→ask on identical prompts), so we don't leave this core behaviour to it.
    # If it wants to plan but we've asked nothing and know nothing about the
    # travellers or preferences, ask one clarifying question first.
    if action == "plan" and asked == 0:
        trav = contract.get("travelers") or []
        knows_access = any((t or {}).get("mobility") for t in trav) or len(trav) > 1
        prefs = contract.get("preferences") or {}
        knows_prefs = bool(contract.get("dietary") or contract.get("budget")
                           or prefs.get("travel_month") or prefs.get("climate")
                           or prefs.get("interests"))
        said_just_plan = any(w in user_msg.lower() for w in
                             ("just plan", "surprise", "no preference", "whatever",
                              "don't care", "dont care", "any", "you decide"))
        if not knows_access and not knows_prefs and not said_just_plan:
            action = "ask"
            # keep the model's question if it wrote one; else a sensible default
            if not message or "?" not in message:
                message = ("Lovely choice! Before I build it — is anyone travelling "
                           "with mobility, stroller, or accessibility needs, and any "
                           "dietary or budget preferences I should plan around?")
            asked += 1

    # persist the turn to short-term memory so context carries across turns
    try:
        mem.save_turn(session_id, user_msg, message)
    except Exception:
        pass

    return {"action": action, "message": message, "contract": contract,
            "asked": asked}


def build_request_from_contract(contract: dict) -> str:
    """Render the accumulated contract into a self-contained planning request
    string that plan_trip / the dossier pipeline can consume — so the existing
    pipeline doesn't need to change; it just receives a well-formed request."""
    dest = contract.get("destination") or "somewhere"
    days = contract.get("trip_length_days")
    parts = [f"Plan a {days} day trip to {dest}" if days else f"Plan a trip to {dest}"]
    trav = contract.get("travelers") or []
    people = []
    for t in trav:
        if (t or {}).get("mobility") == "wheelchair":
            people.append("a wheelchair user")
        elif (t or {}).get("type") == "toddler":
            people.append("a toddler")
        elif (t or {}).get("type") == "senior":
            people.append("a senior")
    if people:
        parts.append("with " + " and ".join(people))
    for d in (contract.get("dietary") or []):
        parts.append(f"({d})")
    if contract.get("budget"):
        parts.append(f"on a {contract['budget']} budget")
    p = contract.get("preferences") or {}
    if p.get("travel_month"):
        parts.append(f"in {p['travel_month']}")
    if p.get("climate"):
        parts.append(f"— note: {p['climate']}")
    # carry special needs into the request so extraction re-captures them
    sn = contract.get("special_needs") or {}
    needs = []
    if sn.get("medical"):
        needs.append("has a medical condition and needs pharmacies and hospitals nearby")
    if sn.get("sensory"):
        needs.append("needs quiet, low-stimulation spaces (sensory/anxiety)")
    if sn.get("heat_sensitive"):
        needs.append("is heat-sensitive and tires easily, needs shade and rest stops")
    if needs:
        parts.append("— traveler " + "; ".join(needs))
    return " ".join(parts)
