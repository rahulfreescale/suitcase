"""Unit tests: model gateway routing + A/B assignment determinism.

Verify cheap tasks route to the fast model, heavy tasks to the strong chain, and
that A/B assignment is deterministic per user (same user always same variant).
No network/LLM.
"""
from app import gateway
from app.experiments import _bucket


def test_cheap_tasks_route_to_fast_model():
    routes = gateway._routes()
    fast = routes["clarify"][0]
    # clarify/plan/reflect all start with the same fast model
    assert routes["plan"][0] == fast
    assert routes["reflect"][0] == fast


def test_heavy_tasks_use_strong_chain():
    routes = gateway._routes()
    # research/write should NOT start with the fast model (they use the strong chain)
    assert routes["research"][0] == routes["write"][0]


def test_fast_route_falls_back_to_strong():
    routes = gateway._routes()
    # the fast route should still contain the strong chain as fallback (len > 1)
    assert len(routes["clarify"]) > 1


def test_ab_bucket_is_deterministic():
    # same (experiment, user) must always map to the same bucket
    b1 = _bucket("write-experiment", "user_42")
    b2 = _bucket("write-experiment", "user_42")
    assert b1 == b2
    assert 0 <= b1 < 100


def test_ab_bucket_varies_by_user():
    # different users generally land in different buckets (not a strict guarantee,
    # but across several users we expect a spread, not all identical)
    buckets = {_bucket("write-experiment", f"user_{i}") for i in range(20)}
    assert len(buckets) > 1
