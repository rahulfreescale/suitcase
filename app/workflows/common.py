"""Shared Temporal constants + a client helper.

Kept tiny and dependency-light so both the worker and the API can import it
without pulling in workflow/activity code.
"""
from __future__ import annotations

import os

# The task queue both the worker and the API agree on. A worker listens on this
# queue; the API starts workflows / sends signals against it.
TASK_QUEUE = "email-approval"

# Where the Temporal server is. `temporal server start-dev` listens here.
def temporal_target() -> str:
    return os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")


async def get_client():
    """Connect to the Temporal server. Import is local so the app doesn't hard-
    depend on temporalio unless the Temporal path is actually used."""
    from temporalio.client import Client
    return await Client.connect(temporal_target())
