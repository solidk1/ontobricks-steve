"""Cooperative cancellation marker.

Raised by long-running worker code
when an external cancel signal has been observed. Callers in the build
pipeline catch this to exit cleanly **without** flipping the task status
to ``failed`` — the task has already transitioned to ``cancelled`` by the
time the exception fires.
"""

from __future__ import annotations

from back.core.errors.OntoBricksError import OntoBricksError


class OperationCancelledError(OntoBricksError):
    """Raised when a worker observes a cancellation request between polls."""

    def __init__(self, message: str = "Operation cancelled", **kw):
        super().__init__(message, status_code=499, **kw)
