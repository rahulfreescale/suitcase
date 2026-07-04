"""Corrections log — the raw signal for the accessibility feedback loop.

Every time someone disagrees with a rating (explicitly via 👍/👎, or implicitly
by dragging a "left out" place back into the plan), we record a correction. An
automated/LLM review can also emit corrections. These are NOT applied to the
bank directly — they're evidence that a later human-reviewed sync job weighs and
promotes (adjusting the bank's confidence). See eval/sync_corrections.

Mirrors the interactions store: DynamoDB, partitioned by UTC date so a daily job
reads one partition cheaply. Local: DynamoDB-Local. Production: Amazon DynamoDB.

Fields per correction:
  place, city          - what the correction is about
  constraint           - which dimension (wheelchair / budget / ...)
  current_label        - what the system rated it
  proposed_label       - what the correction says it should be (may be null for
                         a plain 👎 with no explicit target)
  source               - "user_explicit" | "user_implicit" | "auto_review"
  direction            - "agree" | "disagree"  (agree raises confidence, disagree lowers)
  note                 - optional free text (user's reason, or the auto-check finding)
  status               - "pending" (awaiting human review) | "approved" | "rejected"
"""
from __future__ import annotations
import time
import datetime as dt
import boto3
from boto3.dynamodb.conditions import Key
from app.config import get_settings

_s = get_settings()
TABLE = f"{_s.dynamodb_table}-corrections"


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


def log_correction(place: str, city: str, constraint: str,
                   current_label: str | None, proposed_label: str | None,
                   source: str, direction: str,
                   note: str = "", user_id: str = "anon") -> dict:
    """Record one correction. Never raises into the request path."""
    ts = int(time.time() * 1000)
    item = {
        "date": _today(), "sk": f"{ts}#{place}#{user_id}",
        "ts": ts,
        "place": place or "(unknown)",
        "city": city or "(unknown)",
        "constraint": constraint or "wheelchair",
        "current_label": current_label or "",
        "proposed_label": proposed_label or "",
        "source": source,
        "direction": direction,
        "note": (note or "")[:500],
        "user_id": user_id or "anon",
        "status": "pending",
    }
    try:
        _ddb().Table(TABLE).put_item(Item=item)
    except Exception as e:  # feedback must never break the request
        print(f"[corrections] log failed: {e}")
    return item


def fetch_pending(days: int = 7) -> list[dict]:
    """Corrections awaiting review, most recent first (for the sync/review job)."""
    out = []
    for i in range(days):
        d = (dt.datetime.utcnow() - dt.timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            resp = _ddb().Table(TABLE).query(
                KeyConditionExpression=Key("date").eq(d))
            out.extend([r for r in resp.get("Items", [])
                        if r.get("status") == "pending"])
        except Exception as e:
            print(f"[corrections] fetch failed for {d}: {e}")
    return sorted(out, key=lambda r: r.get("ts", 0), reverse=True)


def set_status(date: str, sk: str, status: str) -> None:
    """Mark a correction approved/rejected after review."""
    try:
        _ddb().Table(TABLE).update_item(
            Key={"date": date, "sk": sk},
            UpdateExpression="SET #s = :v",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":v": status})
    except Exception as e:
        print(f"[corrections] status update failed: {e}")
