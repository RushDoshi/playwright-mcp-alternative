"""FAILED-rail handling (Layer A).

Any unrecoverable exception routes here: persist the error, run cleanup (for regression,
the Azure Spot VM is ALWAYS torn down), and mark the run FAILED.
"""
from __future__ import annotations

from typing import Callable

from .logging import get_logger, run_log

_log = get_logger("errors")


class WorkflowError(Exception):
    """Base for workflow failures that should hit the FAILED rail."""


def to_failed_rail(
    run_id: str,
    store,
    error: Exception,
    cleanup: Callable[[], None] | None = None,
) -> None:
    """Persist FAILED, optionally run mandatory cleanup (VM teardown), never raise."""
    msg = f"{type(error).__name__}: {error}"
    run_log(run_id, "FAIL", msg)
    if cleanup is not None:
        try:
            cleanup()  # e.g. Azure Spot VM teardown — must run even on failure
        except Exception as ce:  # noqa: BLE001 - cleanup must not mask the original error
            _log.error("cleanup after failure also failed: %s", ce)
    try:
        store.fail(run_id, msg)
    except Exception as se:  # noqa: BLE001
        _log.error("could not persist FAILED state: %s", se)
