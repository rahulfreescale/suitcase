"""The durable email-approval workflow.

Flow:
  run()  ->  build_pdf_activity  ->  PARK on wait_condition(decision set)
         ->  approve signal  ->  send_activity  ->  done
         ->  reject signal / timeout  ->  done, no send

Why this is more than a boolean confirm:
  - The 'parked' wait is durable. The workflow's state lives in the Temporal
    server, so if the worker crashes while a request is awaiting approval, the
    pending approval is NOT lost — a worker picks it up exactly where it was.
  - The decision can arrive seconds or days later (wait_condition has a
    timeout you choose), without holding an HTTP request open.
  - status() is queryable, so an admin page can list every parked workflow and
    show approve/reject controls.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

# Import the activity interfaces through the sandbox-safe passthrough.
with workflow.unsafe.imports_passed_through():
    from app.workflows.activities import (
        BuildResult,
        build_pdf_activity,
        send_activity,
    )


@dataclass
class EmailApprovalInput:
    query: str
    recipient: str
    user_id: str | None = None


# How long the workflow will wait for a human decision before giving up.
APPROVAL_TIMEOUT = timedelta(hours=24)


@workflow.defn
class EmailApprovalWorkflow:
    def __init__(self) -> None:
        # None = undecided (parked); "approved" / "rejected" once a signal lands.
        self._decision: str | None = None
        self._state: str = "starting"
        self._destination: str = ""
        self._recipient: str = ""
        self._error: str = ""

    # ---- signals: the human decision arrives here (durably) -----------------
    @workflow.signal
    def approve(self) -> None:
        if self._decision is None:
            self._decision = "approved"

    @workflow.signal
    def reject(self) -> None:
        if self._decision is None:
            self._decision = "rejected"

    # ---- query: the admin page / user page reads live status ----------------
    @workflow.query
    def status(self) -> dict:
        return {
            "state": self._state,
            "recipient": self._recipient,
            "destination": self._destination,
            "decision": self._decision,
            "error": self._error,
        }

    # ---- the workflow body --------------------------------------------------
    @workflow.run
    async def run(self, inp: EmailApprovalInput) -> dict:
        self._recipient = inp.recipient

        # 1. Build the PDF (activity). Retried automatically on transient failure.
        self._state = "building"
        build: BuildResult = await workflow.execute_activity(
            build_pdf_activity,
            args=[inp.query, inp.user_id],
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        self._destination = build.destination

        # 2. PARK — wait for a human approve/reject signal (or time out).
        #    This is the durable pause: state lives in Temporal, not the worker.
        self._state = "pending_approval"
        try:
            await workflow.wait_condition(
                lambda: self._decision is not None,
                timeout=APPROVAL_TIMEOUT,
            )
        except TimeoutError:
            self._state = "expired"
            return {"ok": False, "state": "expired",
                    "reason": "no decision within the approval window"}

        # 3. Act on the decision.
        if self._decision == "rejected":
            self._state = "rejected"
            return {"ok": False, "state": "rejected"}

        self._state = "sending"
        try:
            result = await workflow.execute_activity(
                send_activity,
                args=[inp.recipient, build.destination, build.pdf_b64],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except Exception as e:  # noqa: BLE001 — surface a clean status
            self._state = "send_failed"
            self._error = type(e).__name__
            return {"ok": False, "state": "send_failed", "error": self._error}

        self._state = "sent"
        return {"ok": True, "state": "sent",
                "recipient": result.get("recipient"),
                "status": result.get("status")}
