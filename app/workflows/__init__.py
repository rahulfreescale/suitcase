"""Temporal workflow support for the email-approval feature.

This package holds the durable-workflow version of the email 'human-in-the-loop'
gate. The synchronous /email_itinerary endpoint still exists; this is the
production-grade upgrade the code comments referred to as 'Stage 2':

  user starts a workflow  ->  build the PDF (activity)  ->  PARK, awaiting a
  human decision (a Temporal signal)  ->  on approve, send (activity).

The parked state lives in the Temporal server, not the worker, so a pending
approval survives a worker crash / restart. That durability is the whole point.
"""
