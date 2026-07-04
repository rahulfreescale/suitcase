"""DAILY BATCH JOB — live-traffic evaluation (reference-free).

Reads the last day's logged interactions, samples up to N, and scores them with
RAGAS metrics that need NO ground-truth answer:
  - faithfulness     : is the answer supported by the retrieved context?
  - answer_relevancy : does the answer address the question?

Aggregates, prints, persists a daily summary, and (optionally) pushes scores to
Langfuse and CloudWatch and warns if faithfulness drops below a threshold.

Run on a schedule:
  local : cron -> `python -m eval.live_traffic_eval`
  AWS   : EventBridge Scheduler -> ECS Fargate task (see infra/terraform/scheduled_eval.tf)
"""
from __future__ import annotations
import datetime as dt
import random
from app.config import get_settings
from app.stores.interactions import fetch_recent, save_summary

_s = get_settings()


def _score(samples: list[dict]) -> dict:
    from datasets import Dataset
    from ragas import evaluate
    from ragas.metrics import faithfulness, answer_relevancy
    ds = Dataset.from_list([{
        "question": s["question"],
        "answer": s["answer"],
        "contexts": list(s.get("contexts", ["(none)"])),
    } for s in samples])
    result = evaluate(ds, metrics=[faithfulness, answer_relevancy])
    df = result.to_pandas()
    return {"faithfulness": float(df["faithfulness"].mean()),
            "answer_relevancy": float(df["answer_relevancy"].mean())}


def _push_langfuse(date: str, scores: dict, n: int):
    if not (_s.langfuse_public_key and _s.langfuse_secret_key):
        return
    try:
        from langfuse import Langfuse
        lf = Langfuse(public_key=_s.langfuse_public_key,
                      secret_key=_s.langfuse_secret_key, host=_s.langfuse_host)
        for name, val in scores.items():
            lf.score(name=f"live_{name}", value=val,
                     comment=f"daily live-traffic eval {date} (n={n})")
        lf.flush()
        print("  pushed scores to Langfuse")
    except Exception as e:
        print(f"  langfuse push skipped: {e}")


def _push_cloudwatch(scores: dict):
    if _s.deploy_profile != "aws":
        return
    try:
        import boto3
        cw = boto3.client("cloudwatch", region_name=_s.aws_region)
        cw.put_metric_data(Namespace="Suitcase/LiveEval", MetricData=[
            {"MetricName": k, "Value": v, "Unit": "None"} for k, v in scores.items()])
        print("  pushed metrics to CloudWatch")
    except Exception as e:
        print(f"  cloudwatch push skipped: {e}")


def main():
    date = dt.datetime.utcnow().strftime("%Y-%m-%d")
    interactions = fetch_recent(days=1)
    interactions = [i for i in interactions if i.get("sk") != "SUMMARY"]
    if not interactions:
        print("No interactions found for the last day. "
              "Run `make simulate` first (or wait for real traffic).")
        return

    sample = random.sample(interactions, min(_s.live_eval_sample_size, len(interactions)))
    print(f"Live-traffic eval for {date}: scoring {len(sample)} of "
          f"{len(interactions)} interactions...")

    scores = _score(sample)
    print("\n  Results (reference-free):")
    for k, v in scores.items():
        print(f"    {k:18s} {v:.3f}")

    save_summary(date, scores, len(sample))
    _push_langfuse(date, scores, len(sample))
    _push_cloudwatch(scores)

    if scores["faithfulness"] < _s.live_eval_min_faithfulness:
        print(f"\n  ⚠ ALERT: faithfulness {scores['faithfulness']:.3f} below "
              f"threshold {_s.live_eval_min_faithfulness} — possible hallucinations "
              "in production. Investigate recent traces in Langfuse.")
    else:
        print("\n  Faithfulness within threshold. ✓")


if __name__ == "__main__":
    main()
