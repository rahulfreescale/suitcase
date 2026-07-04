"""Run validated SELECT queries against Amazon Athena (S3 data lake)."""
import time
import boto3
from app.config import get_settings

_s = get_settings()


def run_select(sql: str) -> list[dict]:
    client = boto3.client("athena", region_name=_s.aws_region)
    qid = client.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": _s.athena_database},
        WorkGroup=_s.athena_workgroup,
        ResultConfiguration={"OutputLocation": _s.athena_output_s3},
    )["QueryExecutionId"]

    while True:
        st = client.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]["State"]
        if st in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.6)
    if st != "SUCCEEDED":
        reason = client.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"].get("StateChangeReason", "")
        raise RuntimeError(f"Athena query {st}: {reason}")

    res = client.get_query_results(QueryExecutionId=qid)
    rows = res["ResultSet"]["Rows"]
    header = [c["VarCharValue"] for c in rows[0]["Data"]]
    out = []
    for r in rows[1:]:
        vals = [c.get("VarCharValue") for c in r["Data"]]
        out.append(dict(zip(header, vals)))
    return out
