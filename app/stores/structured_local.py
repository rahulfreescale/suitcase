"""DuckDB stand-in for Athena so the structured path runs with no AWS.

Exposes two tables the LLM is told about — `flights` and `stays` — backed by
local CSVs. SQL is kept ANSI-ish so it ports to Athena/Trino with minimal change.
"""
import duckdb
from app.config import get_settings

_s = get_settings()


def run_select(sql: str) -> list[dict]:
    con = duckdb.connect(database=":memory:")
    con.execute(
        f"CREATE VIEW flights AS "
        f"SELECT * FROM read_csv_auto('{_s.flights_csv}', header=true)"
    )
    con.execute(
        f"CREATE VIEW stays AS "
        f"SELECT * FROM read_csv_auto('{_s.stays_csv}', header=true)"
    )
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
