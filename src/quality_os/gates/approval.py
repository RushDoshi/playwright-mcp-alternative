"""Approval gates (Layer A).

Two human gates, enforced by the host so a run can never skip them or self-approve:

  Gate 1 (coding)     — plan approval IN the Jira ticket   -> AWAITING_APPROVAL
  Gate 2 (regression) — heal + rerun approval              -> AWAITING_RERUN_APPROVAL

A gate pauses the run by persisting an ``awaiting`` payload, then the workflow stops.
The host resumes the run when the human decision arrives.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..state import CodingState, RegressionState, StateStore
from ..utils.logging import run_log


class CodingDecision(str, Enum):
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"


class RerunDecision(str, Enum):
    APPROVE = "approve"   # heal + rerun
    SKIP = "skip"         # report as-is


@dataclass
class GateResult:
    resumed_to: str
    decision: str


def pause_plan_gate(run_id: str, store: StateStore, plan_comment_url: str,
                    comments_baseline: int = 0) -> None:
    """Gate 1: record that we're awaiting plan approval in Jira and stop.

    ``comments_baseline`` is the ticket's comment count right after the plan was
    posted — the Jira approval poller (``gates/jira_poller.py``) uses it to look only
    at comments posted *after* the plan, so a stale "Approved" from an earlier run on
    the same ticket can never auto-approve a fresh plan.
    """
    store.set_awaiting(run_id, {"gate": "plan_approval", "jira_comment": plan_comment_url,
                                "comments_baseline": comments_baseline})
    run_log(run_id, "GATE", "Plan posted to the Jira ticket. Awaiting reviewer approval in Jira "
                            "(reply with the exact word \"Approved\" — polled automatically).")


def resume_plan_gate(run_id: str, store: StateStore, decision: CodingDecision) -> GateResult:
    """Gate 1 resume. Approve -> IMPLEMENTING; request changes -> PLANNING (re-plan loop)."""
    if decision is CodingDecision.APPROVE:
        store.transition(run_id, CodingState.IMPLEMENTING)
        run_log(run_id, "GATE", "Reviewer approved the plan in Jira. Proceeding to implementation.")
        return GateResult(resumed_to=CodingState.IMPLEMENTING.value, decision=decision.value)
    store.transition(run_id, CodingState.PLANNING)
    run_log(run_id, "GATE", "Reviewer requested changes. Re-planning and updating the ticket.")
    return GateResult(resumed_to=CodingState.PLANNING.value, decision=decision.value)


def pause_rerun_gate(run_id: str, store: StateStore, heal_count: int, bug_count: int) -> None:
    """Gate 2: record that we're awaiting heal+rerun approval and stop."""
    store.set_awaiting(
        run_id,
        {"gate": "rerun_approval", "healable": heal_count, "genuine_bugs": bug_count},
    )
    run_log(
        run_id, "GATE",
        f"Awaiting approval: heal {heal_count} flaky test(s) + rerun. "
        f"{bug_count} genuine bug(s) flagged regardless.",
    )


def resume_rerun_gate(run_id: str, store: StateStore, decision: RerunDecision) -> GateResult:
    """Gate 2 resume. Approve -> RERUNNING; skip -> REPORTING (report as-is)."""
    if decision is RerunDecision.APPROVE:
        store.transition(run_id, RegressionState.RERUNNING)
        run_log(run_id, "GATE", "Approved heal + rerun. Re-running healed tests on the same executor.")
        return GateResult(resumed_to=RegressionState.RERUNNING.value, decision=decision.value)
    store.transition(run_id, RegressionState.REPORTING)
    run_log(run_id, "GATE", "Skipped rerun. Reporting results as-is. Genuine bugs remain flagged.")
    return GateResult(resumed_to=RegressionState.REPORTING.value, decision=decision.value)
