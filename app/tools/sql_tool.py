"""Text-to-SQL tool: schema-aware generation, SELECT-only guard, retry loop.

Mirrors the article: inject relevant schema, few-shot examples, always include
key identifier columns, validate (block non-SELECT), cap rows, and self-correct
on execution error up to N attempts.
"""
import sqlglot
from sqlglot import exp
from app.config import get_settings
from app.llm import chat
from app.stores.factory import run_structured_select

_s = get_settings()

# In production, this schema would be fetched/selected dynamically per query.
SCHEMA = (
    "TABLE flights(\n"
    "  flight_id VARCHAR, origin VARCHAR, destination VARCHAR, depart_date DATE,\n"
    "  depart_time VARCHAR, price_usd INT, airline VARCHAR, stops INT,\n"
    "  duration_hours INT, cabin VARCHAR, red_eye VARCHAR('yes'/'no'),\n"
    "  refundable VARCHAR('yes'/'no'), baggage_included VARCHAR('yes'/'no')\n)\n"
    "TABLE stays(\n"
    "  stay_id VARCHAR, city VARCHAR, name VARCHAR, type VARCHAR,\n"
    "  price_per_night_usd INT, rating DOUBLE, neighborhood VARCHAR,\n"
    "  walkable_score INT, family_friendly VARCHAR('yes'/'no'),\n"
    "  wheelchair_accessible VARCHAR('yes'/'no'), has_kitchen VARCHAR('yes'/'no'),\n"
    "  breakfast_included VARCHAR('yes'/'no'), max_occupancy INT\n)"
)

FEWSHOT = """Q: What are the cheapest flights to Tokyo?
SQL: SELECT flight_id, origin, destination, price_usd, airline, stops FROM flights WHERE destination = 'Tokyo' ORDER BY price_usd ASC;
Q: Show me family-friendly, wheelchair-accessible hotels in Lisbon under $200 a night.
SQL: SELECT stay_id, city, name, price_per_night_usd, rating FROM stays WHERE city = 'Lisbon' AND family_friendly = 'yes' AND wheelchair_accessible = 'yes' AND price_per_night_usd < 200 ORDER BY rating DESC;
Q: Which non-stop flights to Barcelona avoid red-eyes?
SQL: SELECT flight_id, origin, destination, price_usd, depart_time FROM flights WHERE destination = 'Barcelona' AND stops = 0 AND red_eye = 'no';
Q: Find apartments in Bangkok with a kitchen that sleep at least 4.
SQL: SELECT stay_id, city, name, price_per_night_usd, max_occupancy FROM stays WHERE city = 'Bangkok' AND has_kitchen = 'yes' AND max_occupancy >= 4;"""

_GEN = """You write {dialect} SQL over this schema:
{schema}

Rules:
- SELECT statements only. Never write/modify data.
- For flights, include destination and price_usd; for stays, include city, name and price_per_night_usd.
- Boolean-like columns (family_friendly, wheelchair_accessible, red_eye, etc.) hold the strings 'yes'/'no'.
- Use only columns that exist.

{fewshot}

Return ONLY the SQL, no prose.
Q: {q}
SQL:"""


def _validate(sql: str) -> str:
    """Ensure single SELECT, enforce row limit. Raises on violation.
    Emits SQL in the dialect of the active backend (duckdb local / trino athena)."""
    out_dialect = "trino" if _s.structured_backend == "athena" else "duckdb"
    statements = sqlglot.parse(sql, read=out_dialect)
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise ValueError("Only a single SELECT statement is permitted.")
    node = statements[0]
    if not node.args.get("limit"):
        node = node.limit(_s.sql_row_limit)
    return node.sql(dialect=out_dialect)


def run_sql(question: str) -> dict:
    dialect = "Trino/Athena" if _s.structured_backend == "athena" else "ANSI"
    messages = [{"role": "user", "content": _GEN.format(
        dialect=dialect, schema=SCHEMA, fewshot=FEWSHOT, q=question)}]
    error_ctx = ""
    for attempt in range(1, _s.sql_max_retries + 1):
        msgs = messages + ([{"role": "user",
                             "content": f"Previous attempt failed: {error_ctx}\nFix and return SQL only."}]
                           if error_ctx else [])
        raw = chat(msgs).strip().strip("`")
        if raw.lower().startswith("sql"):
            raw = raw[3:].strip()
        try:
            safe = _validate(raw)
            rows = run_structured_select(safe)
            return {"tool": "sql", "sql": safe, "rows": rows,
                    "attempts": attempt, "found": bool(rows)}
        except Exception as e:
            error_ctx = f"{type(e).__name__}: {e}"
    return {"tool": "sql", "sql": None, "rows": [],
            "attempts": _s.sql_max_retries, "found": False, "error": error_ctx}
