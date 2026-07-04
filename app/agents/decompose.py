"""Phase 4a.1 - Activity decomposition.

The gap Phase 4a exposed: retrieval returns SECTION-sized guide chunks
("## Getting Around: Prague has an excellent transit network...") but the rater
needs individual ACTIVITIES ("Letna Park - flat, paved, wheelchair accessible").
Rating a whole section against a wheelchair need is meaningless -> everything
scored TOUGH and nothing got placed.

This module unpacks each chunk into candidate activities: named places/things to
do, each carried with the specific sentence(s) that describe it (so the rater
still has real, citable text to judge and lock hard facts against).

Design:
- LLM decomposition (prose -> list of {name, description, is_famous}). The
  description MUST be quoted/near-quoted from the chunk so ratings stay grounded.
- We keep the source chunk's city/page so citations still resolve.
- "Getting Around", "Overview", "Best Time to Visit" sections rarely contain
  rateable ACTIVITIES; the prompt is told to return [] for those rather than
  inventing activities.
- We flag is_famous so Phase 3's skipped-list (famous poor-fits) works, and we
  pass through a section hint so "Popular but Challenging" items can later be
  origin-tagged guide_flagged.
"""
from app import gateway
from app.observability import observe

_PROMPT = """You turn a passage from a city travel guide into a list of distinct
ACTIVITIES or PLACES a visitor could actually DO or GO (parks, museums, landmarks,
markets, attractions, tours, neighborhoods-as-destinations).

Rules:
- Each entry MUST be a physical PLACE or DESTINATION with a location you could
  put on a map (a park, museum, landmark, market, square, neighborhood, venue).
- Do NOT return ACTIONS, RITUALS, or ACTIVITIES-YOU-PERFORM as their own entries
  (e.g. "coin throwing", "photo taking", "people watching", "wish making",
  "shopping", "sunset watching"). Those belong to the place where they happen -
  fold them into that place's entry, never as a standalone item.
- Return ONLY JSON: {{"activities": [{{"name": <short name of the PLACE>,
  "description": <one sentence describing it, quoted or closely paraphrased FROM
  THE PASSAGE ONLY>, "is_famous": <true if a well-known landmark/must-see, else
  false>}}]}}
- The description MUST come from the passage. Do NOT add facts not present.
- If the passage is general logistics/prose with no doable activity (e.g. a
  "Getting Around" transit overview, an "Overview", a "Best Time to Visit"), or
  it's just a section header, return {{"activities": []}}.
- Split distinct places into separate entries. Preserve any accessibility or
  family detail in the description (it's what downstream rating needs).

Passage:
\"\"\"{text}\"\"\""""


@observe(name="decompose_activities")
def decompose_chunk(chunk: dict, user_id: str | None = None) -> list[dict]:
    """One section-chunk -> list of activity dicts {city, page, text, name, is_famous, section_hint}."""
    text = (chunk.get("text") or chunk.get("quote") or "").strip()
    if not text:
        return []
    # cheap heuristic: capture the "## Section" header if present, for origin tagging
    section_hint = None
    first = text.splitlines()[0] if text else ""
    if first.startswith("#"):
        section_hint = first.lstrip("# ").strip()

    try:
        out = gateway.chat_json(
            "clarify",
            [{"role": "user", "content": _PROMPT.format(text=text[:1600])}],
            user_id=user_id,
        )
        items = out.get("activities", []) or []
    except Exception:
        items = []

    activities = []
    for it in items:
        desc = (it.get("description") or "").strip()
        name = (it.get("name") or "").strip()
        if not desc or not name:
            continue
        activities.append({
            "city": chunk.get("city"),
            "page": chunk.get("page"),
            "text": desc,                 # the sentence the rater will judge/cite
            "name": name,
            "is_famous": bool(it.get("is_famous")),
            "section_hint": section_hint,  # e.g. "Popular but Challenging"
        })
    return activities


def decompose_all(chunks: list[dict], user_id: str | None = None) -> list[dict]:
    """Decompose many chunks; dedupe activities by name."""
    seen, out = set(), []
    for ch in chunks:
        for act in decompose_chunk(ch, user_id=user_id):
            key = (act["name"].lower(), (act.get("city") or "").lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(act)
    return out
