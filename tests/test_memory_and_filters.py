"""Unit tests: memory reference resolution + retrieval filter building.

Pure functions, no network/LLM. Verify the travel-domain entity logic works:
'that city' / 'there' resolve to the most-recently-mentioned known city, and
metadata filters are built from the travel fields.
"""
from app.stores.memory import resolve_reference, _last_entities, _canonical_city
from app.stores.vector_opensearch import _filter_clause


# ---- reference resolution -----------------------------------------------------
def _session_with(*texts):
    return {"turns": [{"q": t, "a": ""} for t in texts], "summary": ""}


def test_last_entities_finds_city():
    s = _session_with("What are the best neighborhoods in Lisbon?")
    assert _last_entities(s)["city"] == "Lisbon"


def test_last_entities_most_recent_wins():
    s = _session_with("Tell me about Lisbon", "Now what about Tokyo?")
    assert _last_entities(s)["city"] == "Tokyo"


def test_canonical_city_case_insensitive():
    assert _canonical_city("lisbon") == "Lisbon"
    assert _canonical_city("TOKYO") == "Tokyo"


def test_resolve_that_city():
    s = _session_with("Tell me about Lisbon")
    out = resolve_reference("Is that city walkable?", s)
    assert "Lisbon" in out and "that city" not in out.lower()


def test_resolve_there():
    s = _session_with("Tell me about Tokyo")
    out = resolve_reference("What's the food like there?", s)
    assert "Tokyo" in out


def test_no_reference_left_untouched():
    s = _session_with("Tell me about Lisbon")
    q = "What are the best beaches in Barcelona?"
    assert resolve_reference(q, s) == q  # no back-reference -> unchanged


def test_resolve_with_empty_session():
    assert resolve_reference("Is it walkable there?", {}) == "Is it walkable there?"


# ---- retrieval filter building ------------------------------------------------
def test_filter_clause_single_field():
    out = _filter_clause({"city": "Lisbon"})
    assert out == [{"term": {"city": "Lisbon"}}]


def test_filter_clause_multi_field():
    out = _filter_clause({"city": "Lisbon", "region": "Europe"})
    assert {"term": {"city": "Lisbon"}} in out
    assert {"term": {"region": "Europe"}} in out


def test_filter_clause_empty():
    assert _filter_clause({}) == []
    assert _filter_clause(None) == []


def test_filter_clause_skips_empty_values():
    out = _filter_clause({"city": "Lisbon", "country": ""})
    assert out == [{"term": {"city": "Lisbon"}}]
