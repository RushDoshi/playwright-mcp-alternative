"""State machines and persistence for both workflows (Layer A).

The host owns state. Transitions are validated here so a workflow can never jump a step
or skip a gate. State is persisted to ``state.json`` per run so a gate can pause a run
and resume it later.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class CodingState(str, Enum):
    PENDING = "PENDING"
    CLONING = "CLONING"
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    IMPLEMENTING = "IMPLEMENTING"
    CREATING_PR = "CREATING_PR"
    DONE = "DONE"
    FAILED = "FAILED"


class RegressionState(str, Enum):
    QUEUED = "QUEUED"
    PROVISIONING = "PROVISIONING"
    RUNNING = "RUNNING"
    ANALYZING = "ANALYZING"
    AWAITING_RERUN_APPROVAL = "AWAITING_RERUN_APPROVAL"
    RERUNNING = "RERUNNING"
    REPORTING = "REPORTING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


# Legal forward transitions. FAILED is reachable from any non-terminal state.
CODING_TRANSITIONS: dict[CodingState, set[CodingState]] = {
    CodingState.PENDING: {CodingState.CLONING},
    CodingState.CLONING: {CodingState.PLANNING},
    CodingState.PLANNING: {CodingState.AWAITING_APPROVAL},
    # re-plan loop: AWAITING -> PLANNING on "request changes"; -> IMPLEMENTING on approve
    CodingState.AWAITING_APPROVAL: {CodingState.PLANNING, CodingState.IMPLEMENTING},
    CodingState.IMPLEMENTING: {CodingState.CREATING_PR},
    CodingState.CREATING_PR: {CodingState.DONE},
    CodingState.DONE: set(),
    CodingState.FAILED: set(),
}

REGRESSION_TRANSITIONS: dict[RegressionState, set[RegressionState]] = {
    RegressionState.QUEUED: {RegressionState.PROVISIONING},
    RegressionState.PROVISIONING: {RegressionState.RUNNING},
    RegressionState.RUNNING: {RegressionState.ANALYZING},
    RegressionState.ANALYZING: {RegressionState.AWAITING_RERUN_APPROVAL},
    # approve -> RERUNNING; skip -> REPORTING
    RegressionState.AWAITING_RERUN_APPROVAL: {RegressionState.RERUNNING, RegressionState.REPORTING},
    RegressionState.RERUNNING: {RegressionState.REPORTING},
    RegressionState.REPORTING: {RegressionState.COMPLETE},
    RegressionState.COMPLETE: set(),
    RegressionState.FAILED: set(),
}


class InvalidTransition(Exception):
    """Raised when an illegal state transition is attempted."""


# Every state a run can never leave. Shared by _write()'s archiving decision
# and supersede_open_runs_for_ticket()'s "don't touch a finished run" check —
# one definition so the two can't drift apart.
_TERMINAL_STATUSES = {CodingState.DONE.value, CodingState.FAILED.value,
                      RegressionState.COMPLETE.value, RegressionState.FAILED.value}


def _allowed(current: Any, nxt: Any) -> bool:
    if isinstance(current, CodingState):
        table = CODING_TRANSITIONS
        failed = CodingState.FAILED
    else:
        table = REGRESSION_TRANSITIONS
        failed = RegressionState.FAILED
    if nxt is failed and current not in (failed,) and table.get(current):
        return True  # FAILED reachable from any non-terminal state
    if nxt is failed and not table.get(current):
        return False  # already terminal
    return nxt in table.get(current, set())


@dataclass
class RunRecord:
    run_id: str
    workflow: str  # "coding" | "regression"
    status: str
    ticket: str | None = None
    history: list[dict[str, str]] = field(default_factory=list)
    awaiting: dict[str, Any] | None = None  # gate pause payload
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class StateStore:
    """Thread-safe JSON-backed store, one file per run.

    Active (non-terminal) runs live directly in ``state_dir``; a run is moved
    into ``state_dir/_archive`` the moment it reaches a terminal status (DONE/
    FAILED/COMPLETE). Every run this store has EVER created otherwise stays in
    the flat top-level directory forever — at hundreds/thousands of tickets
    accumulated over time, `list_awaiting()` (the Gate-1 poller, every 30s) and
    `supersede_open_runs_for_ticket()` (every new `/run/uitest` trigger) each
    glob and JSON-parse every one of those files just to find the handful that
    are actually still active, an O(all runs ever) cost paid on every call.
    Archiving keeps both scans to O(active runs), which stays small regardless
    of how many tickets have been processed in total. ``load()`` transparently
    checks both locations, so callers never need to know or care where a given
    run currently lives.
    """

    def __init__(self, state_dir: str | Path) -> None:
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir = self.dir / "_archive"
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, run_id: str) -> Path:
        """Where run_id's file currently lives. Checked active-dir-first since
        that's the far more common case (most calls concern an in-flight run);
        falls back to the archive for a run that's already finished, and to
        the (not yet existing) active path for a brand new run_id, so writing
        a fresh run still lands in the normal place."""
        active = self.dir / f"{run_id}.json"
        if active.exists():
            return active
        archived = self.archive_dir / f"{run_id}.json"
        if archived.exists():
            return archived
        return active

    def create(self, run_id: str, workflow: str, ticket: str | None = None) -> RunRecord:
        initial = CodingState.PENDING if workflow == "coding" else RegressionState.QUEUED
        rec = RunRecord(run_id=run_id, workflow=workflow, status=initial.value, ticket=ticket)
        rec.history.append({"status": rec.status})
        self._write(rec)
        return rec

    def load(self, run_id: str) -> RunRecord:
        with self._lock:
            raw = json.loads(self._path(run_id).read_text())
        return RunRecord(**raw)

    def _write(self, rec: RunRecord) -> None:
        with self._lock:
            is_terminal = rec.status in _TERMINAL_STATUSES
            target_dir = self.archive_dir if is_terminal else self.dir
            other_dir = self.dir if is_terminal else self.archive_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / f"{rec.run_id}.json").write_text(json.dumps(asdict(rec), indent=2))
            # A run only ever moves active -> archive, never back (no transition
            # table permits leaving a terminal state) -- so the only stale copy
            # that can ever exist in the other location is the pre-archive one,
            # removed here so list_awaiting()/supersede...() never see it twice.
            stale = other_dir / f"{rec.run_id}.json"
            if stale.exists():
                stale.unlink()

    def transition(self, run_id: str, nxt: CodingState | RegressionState) -> RunRecord:
        rec = self.load(run_id)
        current = _coerce(rec.workflow, rec.status)
        if not _allowed(current, nxt):
            raise InvalidTransition(f"{rec.workflow}: {current.value} -> {nxt.value} is not allowed")
        rec.status = nxt.value
        rec.history.append({"status": nxt.value})
        rec.awaiting = None
        self._write(rec)
        return rec

    def set_awaiting(self, run_id: str, payload: dict[str, Any]) -> RunRecord:
        rec = self.load(run_id)
        rec.awaiting = payload
        self._write(rec)
        return rec

    def update_data(self, run_id: str, **kv) -> RunRecord:
        """Public API to persist workflow context (clone path, plan, VM name, ...)."""
        rec = self.load(run_id)
        rec.data.update(kv)
        self._write(rec)
        return rec

    def list_awaiting(self, gate: str) -> list[RunRecord]:
        """All runs currently parked at the given gate (e.g. ``"plan_approval"``).

        Used by the Jira approval poller to find every run it needs to check —
        generic across tickets, no separate ticket->run index needed.
        """
        awaiting_statuses = {CodingState.AWAITING_APPROVAL.value,
                            RegressionState.AWAITING_RERUN_APPROVAL.value}
        out: list[RunRecord] = []
        # Only the active directory is scanned -- a run that's already
        # terminal has been moved into _archive by _write() and can never be
        # "awaiting" anyway, so it no longer costs a read+parse here just to
        # be filtered back out. TypeError is caught alongside the JSON
        # errors because this directory isn't guaranteed to hold only
        # RunRecord-shaped files -- e.g. JiraReadyPoller's own
        # ready_poller_seen.json ({ticket: hash}) lives in this same
        # directory and would otherwise blow up RunRecord(**raw) with an
        # unrelated TypeError, silently killing every poll cycle whenever
        # both pollers are enabled together.
        for p in self.dir.glob("*.json"):
            try:
                rec = self.load(p.stem)
            except (FileNotFoundError, json.JSONDecodeError, TypeError):
                continue
            # status is the source of truth; a stale `awaiting` payload left
            # over on an already-terminal run (e.g. from before this status
            # check existed) must not resurrect it.
            if rec.status not in awaiting_statuses:
                continue
            if rec.awaiting and rec.awaiting.get("gate") == gate:
                out.append(rec)
        return out

    def supersede_open_runs_for_ticket(self, ticket_key: str, except_run_id: str,
                                       prefix: str) -> list[str]:
        """Fail every non-terminal run for ``ticket_key`` whose run_id starts with
        ``prefix`` (e.g. "ui-" or "code-"), other than ``except_run_id``.

        Every ``/run/uitest`` (or webhook) call mints a brand new run_id with no
        dedup against a prior, still-open run for the same ticket -- so a ticket
        re-triggered several times (e.g. while diagnosing a failure) piles up
        multiple runs legitimately parked at AWAITING_APPROVAL, none of them ever
        explicitly failed. JiraApprovalPoller.list_awaiting() then matches ALL of
        them against the next "Approved" comment and resumes every one
        concurrently -- multiple Playwright processes rendering and racing to
        execute the SAME spec file path at once, which is exactly what caused
        several browsers to launch simultaneously and crash into each other on
        in practice. ``prefix`` scopes this to same-kind runs only, so
        starting a new UI-test run never supersedes a legitimately-concurrent
        coding-agent run for the same ticket (by design, both can run at once).
        """
        # Also active-directory-only (see list_awaiting's docstring above) --
        # a terminal run has already been archived out of here, so this again
        # only ever reads the runs that could actually still need superseding.
        superseded: list[str] = []
        for p in self.dir.glob(f"{prefix}*.json"):
            run_id = p.stem
            if run_id == except_run_id:
                continue
            try:
                rec = self.load(run_id)
            except (FileNotFoundError, json.JSONDecodeError, TypeError):
                continue
            if rec.ticket == ticket_key and rec.status not in _TERMINAL_STATUSES:
                self.fail(run_id, f"superseded by newer run {except_run_id} for {ticket_key}")
                superseded.append(run_id)
        return superseded

    def fail(self, run_id: str, error: str) -> RunRecord:
        rec = self.load(run_id)
        # never overwrite a successful terminal state
        if rec.status in (CodingState.DONE.value, RegressionState.COMPLETE.value):
            return rec
        failed = CodingState.FAILED if rec.workflow == "coding" else RegressionState.FAILED
        rec.status = failed.value
        rec.error = error
        rec.history.append({"status": failed.value})
        # a run parked at a gate that then fails must stop being "awaiting" —
        # otherwise list_awaiting() (the Jira approval poller) keeps finding
        # this dead run forever and repeatedly tries (and fails) to resume it
        # whenever it sees an unrelated "Approved" reply on the same ticket.
        rec.awaiting = None
        self._write(rec)
        return rec


def _coerce(workflow: str, status: str) -> CodingState | RegressionState:
    return CodingState(status) if workflow == "coding" else RegressionState(status)
