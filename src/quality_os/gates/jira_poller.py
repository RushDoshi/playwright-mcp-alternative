"""Jira pollers — Layer A.

Two independent pull-based listeners, both generic across every ticket and both
workflows (Coding Agent, UI-Test Agent) — neither has ticket-specific logic.

``JiraApprovalPoller`` — Gate-1 auto-resume. The approval gate itself (pause/resume
state transitions) lives in ``gates/approval.py``; this watches Jira for the human's
reply and calls the resume automatically, so nobody has to hit the ``/decision``
endpoint by hand. Detection rule: a run parked at Gate 1 (``AWAITING_APPROVAL`` /
``plan_approval``) is auto-approved the moment a comment posted *after* the plan
appears whose trimmed text is exactly the word "Approved" (case-insensitive).
Comments from before the plan was posted are ignored via the ``comments_baseline``
recorded at pause time (see ``approval.pause_plan_gate``) — this stops a stale
"Approved" from a previous run on the same ticket from auto-approving a brand new plan.

``JiraReadyPoller`` — pull-based trigger, for hosts with no Jira Automation rule
configured to POST to ``/webhook/jira``. Polls a JQL search for candidate tickets and
starts the pipeline for any ticket that's new or changed since it last triggered.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import re
import threading
from collections.abc import Callable
from pathlib import Path

from ..clients.jira_client import JiraClient
from ..state import StateStore
from ..utils.logging import get_logger

logger = get_logger("jira_poller")

_EXACT_APPROVED_RE = re.compile(r"^\s*approved\s*$", re.I)


def is_exact_approval(comment_text: str) -> bool:
    """True if a comment's full text is (only) the word "Approved", any case."""
    return bool(_EXACT_APPROVED_RE.match(comment_text or ""))


class JiraApprovalPoller:
    """Every ``interval_seconds``, checks all Gate-1-paused runs for an exact
    "Approved" reply on their ticket and resumes them via ``on_approved``.

    Checks up to ``max_concurrent_checks`` runs' tickets against Jira at once
    (bounded thread pool), not one at a time — with hundreds/thousands of
    tickets parked at Gate 1 simultaneously, a strictly sequential loop of
    Jira round-trips can take far longer than ``interval_seconds`` itself to
    get through a single cycle, permanently falling behind. ``on_approved``
    itself is expected to stay fast (the real workflows already hand the long
    work off to their own background thread — see ``main.py``'s
    ``_auto_resume``), so bounding this only guards against overwhelming
    Jira's API, not against slow downstream work.
    """

    def __init__(self, store: StateStore, jira: JiraClient,
                on_approved: Callable[[str], None], interval_seconds: int = 30,
                max_concurrent_checks: int = 10) -> None:
        self.store = store
        self.jira = jira
        self.on_approved = on_approved
        self.interval = max(5, interval_seconds)
        self.max_concurrent_checks = max(1, max_concurrent_checks)
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        """Drive the poll loop off the event loop thread so a slow Jira call or a
        long-running resume (e.g. a Playwright run) never blocks the HTTP server."""
        logger.info(f"Jira approval poller started (every {self.interval}s)")
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.poll_once)
            except Exception:  # noqa: BLE001
                logger.exception("Jira approval poll failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    def poll_once(self) -> None:
        awaiting = [rec for rec in self.store.list_awaiting(gate="plan_approval") if rec.ticket]
        if not awaiting:
            return
        # A bounded pool, not one call per run in serial: each check is one
        # blocking Jira HTTP round-trip, and this waits for the whole batch
        # (concurrent.futures.wait) before returning, same as the old
        # loop -- callers (including every existing test) still see every
        # side effect completed by the time poll_once() returns.
        workers = min(self.max_concurrent_checks, len(awaiting))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self._safe_check_run, rec.run_id, rec.ticket, rec.awaiting or {})
                      for rec in awaiting]
            concurrent.futures.wait(futures)

    def _safe_check_run(self, run_id: str, ticket_key: str, awaiting: dict) -> None:
        try:
            self._check_run(run_id, ticket_key, awaiting)
        except Exception:  # noqa: BLE001
            logger.exception(f"approval check failed for {run_id} ({ticket_key})")

    def _check_run(self, run_id: str, ticket_key: str, awaiting: dict) -> None:
        baseline = int(awaiting.get("comments_baseline", 0) or 0)
        ticket = self.jira.fetch_ticket(ticket_key)
        comments = ticket.comments_newest_first  # newest first
        new_count = max(0, len(comments) - baseline)
        for text in comments[:new_count]:
            if is_exact_approval(text):
                logger.info(f"exact 'Approved' seen on {ticket_key} — auto-resuming {run_id}")
                self.on_approved(run_id)
                return


def _ticket_hash(requirement_text: str) -> str:
    """Same hash basis as ``uitest/designer.py::design_scenarios``'s cache key —
    one consistent definition of "the ticket changed" across the codebase."""
    return hashlib.sha1(requirement_text.encode("utf-8")).hexdigest()


class _SeenStore:
    """Tiny persisted ``{ticket_key: last_seen_hash}`` map — not a run record,
    so it lives in its own file rather than extending ``StateStore``."""

    def __init__(self, state_dir: str | Path) -> None:
        self.path = Path(state_dir) / "ready_poller_seen.json"
        self._lock = threading.Lock()

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return {}

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._load().get(key)

    def set(self, key: str, value: str) -> None:
        with self._lock:
            data = self._load()
            data[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=2))


class JiraReadyPoller:
    """Pull-based alternative to ``POST /webhook/jira`` for hosts with no Jira
    Automation rule configured to push status changes to us. Every
    ``interval_seconds``, searches ``jql`` for candidate tickets and calls
    ``on_ready(key)`` for any ticket that's new or whose content changed since
    it was last seen — so a ticket sitting unchanged in "Ready for
    Development" is polled harmlessly, but editing it (e.g. adding negative
    scenarios) and flipping the status back to Ready reliably re-triggers.
    """

    def __init__(self, store: StateStore, jira: JiraClient, jql: str,
                on_ready: Callable[[str], None], interval_seconds: int = 60) -> None:
        self.jira = jira
        self.jql = jql
        self.on_ready = on_ready
        self.interval = max(5, interval_seconds)
        self.seen = _SeenStore(store.dir)
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        logger.info(f"Jira ready-poller started (every {self.interval}s, jql={self.jql!r})")
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.poll_once)
            except Exception:  # noqa: BLE001
                logger.exception("Jira ready-poll failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    def poll_once(self) -> None:
        for key in self.jira.search_issues(self.jql):
            try:
                self._check_ticket(key)
            except Exception:  # noqa: BLE001
                logger.exception(f"ready-poll check failed for {key}")

    def _check_ticket(self, key: str) -> None:
        ticket = self.jira.fetch_ticket(key)
        current_hash = _ticket_hash(ticket.requirement_text())
        if self.seen.get(key) == current_hash:
            return  # unchanged since last trigger — nothing to do
        logger.info(f"{key} is new or changed since last trigger — starting pipeline")
        self.seen.set(key, current_hash)
        self.on_ready(key)
