"""DynamoDB app-level state: intermediate steps, citations, logs.

Distinct from the LangGraph *agent* state (Postgres checkpointer). This is the
human-facing trail shown in the UI and used for debugging.
"""
import time
import boto3
from boto3.dynamodb.conditions import Key
from app.config import get_settings

_s = get_settings()


def _ddb_kwargs() -> dict:
    """boto3 kwargs. For DynamoDB Local (endpoint set), supply dummy credentials
    so the app runs even when no AWS credentials are configured."""
    kw = {"region_name": _s.aws_region}
    if _s.dynamodb_endpoint:
        kw["endpoint_url"] = _s.dynamodb_endpoint
        kw["aws_access_key_id"] = "local"
        kw["aws_secret_access_key"] = "local"
    return kw


def _table():
    return boto3.resource("dynamodb", **_ddb_kwargs()).Table(_s.dynamodb_table)


def create_table() -> None:
    ddb = boto3.resource("dynamodb", **_ddb_kwargs())
    existing = [t.name for t in ddb.tables.all()]
    if _s.dynamodb_table in existing:
        print(f"table '{_s.dynamodb_table}' already exists")
        return
    ddb.create_table(
        TableName=_s.dynamodb_table,
        KeySchema=[{"AttributeName": "thread_id", "KeyType": "HASH"},
                   {"AttributeName": "step_id", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "thread_id", "AttributeType": "S"},
                              {"AttributeName": "step_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    ).wait_until_exists()
    print(f"created table '{_s.dynamodb_table}'")


def log_step(thread_id: str, step_id: str, payload: dict) -> None:
    try:
        _table().put_item(Item={"thread_id": thread_id, "step_id": step_id,
                                "ts": int(time.time() * 1000), **payload})
    except Exception as e:  # never let telemetry break the workflow
        print(f"[appstate] log_step failed: {e}")


def get_trail(thread_id: str) -> list[dict]:
    resp = _table().query(
        KeyConditionExpression=Key("thread_id").eq(thread_id))
    return sorted(resp.get("Items", []), key=lambda x: x.get("ts", 0))
