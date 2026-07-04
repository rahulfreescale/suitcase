"""Production interaction log — the source data for live-traffic evaluation.

Every answered request is recorded with exactly the fields needed to score it
later WITHOUT a reference answer: the question, the answer, and the retrieved
contexts. Records are partitioned by UTC date so the daily batch can read a
single day cheaply (one DynamoDB partition).

Local: DynamoDB-Local.  Production: Amazon DynamoDB.
In a larger system you might instead stream these to S3 (a data lake) and query
them with Athena — same idea, same fields.
"""
from __future__ import annotations
import time
import datetime as dt
import boto3
from boto3.dynamodb.conditions import Key
from app.config import get_settings

_s = get_settings()
TABLE = f"{_s.dynamodb_table}-interactions"


def _ddb():
    kw = {"region_name": _s.aws_region}
    if _s.dynamodb_endpoint:
        kw["endpoint_url"] = _s.dynamodb_endpoint
        kw["aws_access_key_id"] = "local"
        kw["aws_secret_access_key"] = "local"
    return boto3.resource("dynamodb", **kw)


def create_table() -> None:
    ddb = _ddb()
    if TABLE in [t.name for t in ddb.tables.all()]:
        print(f"table '{TABLE}' already exists")
        return
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "date", "KeyType": "HASH"},
                   {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "date", "AttributeType": "S"},
                              {"AttributeName": "sk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    ).wait_until_exists()
    print(f"created table '{TABLE}'")


def _today() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%d")


def log_interaction(thread_id: str, question: str, answer: str,
                    contexts: list[str]) -> None:
    """Record one production interaction for later evaluation."""
    ts = int(time.time() * 1000)
    item = {
        "date": _today(), "sk": f"{ts}#{thread_id}",
        "thread_id": thread_id, "ts": ts,
        "question": question or "(empty)",
        "answer": answer or "(empty)",
        "contexts": [c for c in (contexts or []) if c][:20] or ["(none)"],
    }
    try:
        _ddb().Table(TABLE).put_item(Item=item)
    except Exception as e:  # logging must never break the request
        print(f"[interactions] log failed: {e}")


def fetch_for_date(date: str) -> list[dict]:
    try:
        resp = _ddb().Table(TABLE).query(KeyConditionExpression=Key("date").eq(date))
        return resp.get("Items", [])
    except Exception as e:
        print(f"[interactions] fetch failed for {date}: {e}")
        return []


def fetch_recent(days: int = 1) -> list[dict]:
    """All interactions from the last `days` UTC days (today + previous)."""
    out: list[dict] = []
    base = dt.datetime.utcnow()
    for d in range(days):
        date = (base - dt.timedelta(days=d)).strftime("%Y-%m-%d")
        out.extend(fetch_for_date(date))
    return out


def save_summary(date: str, scores: dict, n: int) -> None:
    """Persist a daily eval summary back into the same table."""
    try:
        _ddb().Table(TABLE).put_item(Item={
            "date": date, "sk": "SUMMARY",
            "thread_id": "__live_eval__", "ts": int(time.time() * 1000),
            "n_scored": n,
            **{f"score_{k}": str(round(v, 4)) for k, v in scores.items()},
        })
    except Exception as e:
        print(f"[interactions] summary save failed: {e}")
