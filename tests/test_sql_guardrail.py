"""Unit tests: Text-to-SQL safety validation.

The SELECT-only guardrail is security-critical — it must block any non-SELECT
(INSERT/UPDATE/DELETE/DROP) and reject multi-statement injection. These run
without a database or LLM.
"""
import pytest
from app.tools.sql_tool import _validate


def test_allows_simple_select():
    out = _validate("SELECT city, name FROM stays WHERE city = 'Lisbon'")
    assert out.lower().startswith("select")


def test_adds_row_limit_when_missing():
    out = _validate("SELECT * FROM stays")
    assert "limit" in out.lower()


def test_blocks_insert():
    with pytest.raises(ValueError):
        _validate("INSERT INTO stays VALUES ('x')")


def test_blocks_update():
    with pytest.raises(ValueError):
        _validate("UPDATE stays SET price_per_night_usd = 0")


def test_blocks_delete():
    with pytest.raises(ValueError):
        _validate("DELETE FROM stays")


def test_blocks_drop():
    with pytest.raises(ValueError):
        _validate("DROP TABLE stays")


def test_blocks_multi_statement_injection():
    # classic injection: a benign SELECT followed by a destructive statement
    with pytest.raises(ValueError):
        _validate("SELECT * FROM stays; DROP TABLE stays")
