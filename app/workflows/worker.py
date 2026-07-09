"""A Temporal worker: runs the email-approval workflow + its activities.

Run one (or several — they're horizontally scalable with no code change,
because Temporal holds the workflow state, not the worker):

    python3 -m app.workflows.worker

Needs a Temporal server running:  temporal server start-dev

Kill this worker while a request is parked awaiting approval, and nothing is
lost — the parked workflow survives in the Temporal server and any worker
resumes it when the approve signal arrives. That's the durability demo.
"""
from __future__ import annotations

import asyncio

from temporalio.worker import Worker

from app.workflows.common import TASK_QUEUE, get_client
from app.workflows.activities import build_pdf_activity, send_activity
from app.workflows.email_approval import EmailApprovalWorkflow


async def main() -> None:
    client = await get_client()
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[EmailApprovalWorkflow],
        activities=[build_pdf_activity, send_activity],
    )
    print(f"[temporal-worker] listening on task queue '{TASK_QUEUE}' "
          f"— workflow + 2 activities registered. Ctrl+C to stop.")
    await worker.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[temporal-worker] stopped.")
