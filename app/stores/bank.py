"""Accessibility bank reader.

Loads the precomputed per-place accessibility bank (data/banks/<City>_accessibility.csv)
and matches a retrieved activity to a bank row so the rater can use the stored,
confidence-scored rating instead of re-deriving from thin guide prose.

Matching: exact name first, then fuzzy (normalized token overlap), so
"the Charles Bridge" / "Charles Bridge" / "Charles Bridge (Karluv Most)" all hit
the same row. Returns None on a miss so the caller can fall back to RAG/prose.

Bank row shape (from build_bank / the researched CSV):
  city, place, is_famous, wheelchair, toddler, senior, confidence, note, source
"""
import csv
import re
import unicodedata
from pathlib import Path
from functools import lru_cache

_BANK_DIR = Path("data/banks")


def _norm(s: str) -> set:
    """Normalize a place name to a set of significant tokens for fuzzy matching."""
    s = (s or "").lower()
    # strip accents: "letná" -> "letna", "malá" -> "mala"
    s = "".join(c for c in unicodedata.normalize("NFKD", s)
                if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9 ]", " ", s)              # drop punctuation/parens
    stop = {"the", "a", "an", "of", "and", "de", "la", "el"}
    return {w for w in s.split() if len(w) > 2 and w not in stop}


@lru_cache(maxsize=32)
def _load_city(city: str) -> tuple:
    """Load a city's bank rows once (cached). Returns tuple of dicts."""
    path = _BANK_DIR / f"{city.replace(' ', '_')}_accessibility.csv"
    if not path.exists():
        return tuple()
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            r["_tokens"] = _norm(r.get("place", ""))
            rows.append(r)
    return tuple(rows)


def lookup(place_name: str, city: str):
    """Find the bank row best matching this place in this city, or None.

    1. exact (normalized) name equality
    2. fuzzy: highest token-overlap (Jaccard), requiring a real overlap
    """
    rows = _load_city(city)
    if not rows:
        return None
    want = _norm(place_name)
    if not want:
        return None

    # exact normalized match
    for r in rows:
        if r["_tokens"] == want:
            return r

    # fuzzy: best Jaccard overlap, with a floor so weak matches don't count
    best, best_score = None, 0.0
    for r in rows:
        toks = r["_tokens"]
        if not toks:
            continue
        inter = len(want & toks)
        if inter == 0:
            continue
        union = len(want | toks)
        score = inter / union
        # also accept strong containment (activity name inside bank place or vice versa)
        contained = inter == len(want) or inter == len(toks)
        eff = max(score, 0.6 if contained else 0.0)
        if eff > best_score:
            best, best_score = r, eff

    return best if best_score >= 0.5 else None


def has_bank(city: str) -> bool:
    return bool(_load_city(city))


def list_places(city: str) -> list:
    """Return every place in the city's bank as an activity dict.

    The bank IS the curated catalog of a city's real places, so it's the
    authoritative source of candidate activities - not whatever retrieval
    happens to surface. Each returned item is shaped for the rater/decomposer
    path: {city, name, is_famous, text}.
    """
    rows = _load_city(city)
    out = []
    for r in rows:
        out.append({
            "city": r.get("city") or city,
            "name": r.get("place"),
            "is_famous": str(r.get("is_famous", "")).strip().lower() in ("true", "1", "yes"),
            "text": (r.get("note") or "").strip(),   # bank note as description
            "section_hint": None,
            "from_bank": True,
        })
    return out


# ---------- LLM fallback: only fires when fuzzy finds nothing ----------

def _llm_match(place_name: str, city: str, user_id=None):
    """Resolve a place to a bank row using the LLM, for cases fuzzy can't handle
    (synonyms, translations like 'Karluv most'->Charles Bridge, or a description
    instead of a name). Returns the row or None. Escalation path only - callers
    should try fuzzy `lookup` first."""
    rows = _load_city(city)
    if not rows:
        return None
    # local import to avoid a hard dependency for the pure-fuzzy path/tests
    try:
        from app import gateway
    except Exception:
        return None

    catalog = [r.get("place", "") for r in rows]
    numbered = "\n".join(f"{i}. {p}" for i, p in enumerate(catalog))
    prompt = (
        "Match the described place to ONE entry in the list, or NONE if it "
        "clearly isn't any of them. Consider synonyms and translations "
        "(e.g. 'Karluv most' = 'Charles Bridge').\n\n"
        f"City: {city}\nPlace to match: \"{place_name}\"\n\n"
        f"Bank entries:\n{numbered}\n\n"
        'Return ONLY JSON: {"index": <number or -1>}'
    )
    try:
        out = gateway.chat_json("clarify",
            [{"role": "user", "content": prompt}], user_id=user_id)
        idx = int(out.get("index", -1))
    except Exception:
        return None
    if 0 <= idx < len(rows):
        return rows[idx]
    return None


def resolve(place_name: str, city: str, user_id=None, allow_llm: bool = True):
    """Best-effort match: cheap fuzzy first, LLM only on a miss.

    This is the entry point callers should use. It embodies the project's
    cheap-first / escalate-on-miss principle: fuzzy handles the easy majority
    for free and deterministically; the LLM is spent only where fuzzy fails.
    """
    hit = lookup(place_name, city)
    if hit is not None:
        return hit, "fuzzy"
    if allow_llm:
        hit = _llm_match(place_name, city, user_id=user_id)
        if hit is not None:
            return hit, "llm"
    return None, "miss"
