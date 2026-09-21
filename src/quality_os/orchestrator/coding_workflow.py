"""Coding workflow driver — REFERENCE STUB (Layer A).

The production system pairs the UI-Test workflow with a Coding Agent that clones
a repo, plans a change in the Jira ticket, waits at the same human approval gate,
implements it via `claude -p`, and opens a PR. That agent is not part of this
open-source release.

This stub drives the SAME state machine and the SAME Gate-1 approval flow as the
real workflow (see orchestrator/uitest_workflow.py for the full implementation
pattern) so that:
  * the host boots and every /coding + /webhook/jira endpoint works,
  * a run progresses through the real states and can pause/resume at Gate 1,
  * nothing is left stuck.
It performs no real code changes. Replace the marked bodies to implement a real
coding agent, mirroring uitest_workflow.py.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..clients.bitbucket_client import BitbucketClient
from ..clients.claude_runner import ClaudeRunner
from ..clients.jira_client import JiraClient
from ..config import Settings
from ..gates import approval
from ..state import CodingState, StateStore
from ..utils.errors import to_failed_rail
from ..utils.logging import run_log


@dataclass
class CodingDeps:
    settings: Settings
    store: StateStore
    jira: JiraClient
    bitbucket: BitbucketClient
    claude: ClaudeRunner | None = None


class CodingWorkflow:
    """PENDING -> CLONING -> PLANNING -> AWAITING_APPROVAL (Gate 1)
              -> IMPLEMENTING -> CREATING_PR -> DONE   (+ FAILED rail)."""

    def __init__(self, deps: CodingDeps) -> None:
        self.d = deps

    def start(self, run_id: str, ticket_key: str) -> None:
        store = self.d.store
        try:
            run_log(run_id, "JIRA", f"Ticket {ticket_key} -> Ready for Development.")
            store.transition(run_id, CodingState.CLONING)
            run_log(run_id, "WORKSPACE", f"[STUB] workspace ready for {ticket_key}")

            store.transition(run_id, CodingState.PLANNING)
            plan = (f"# Plan for {ticket_key}\n\n"
                    "_Reference coding-agent stub — no real code change is made._\n\n"
                    "Approve this plan to advance the run to completion.")
            run_log(run_id, "PLANNER", "[STUB] plan generated.")

            store.transition(run_id, CodingState.AWAITING_APPROVAL)
            try:
                comment_url = self.d.jira.post_plan_comment(ticket_key, plan)
                baseline = self.d.jira.comment_count(ticket_key)
            except Exception:  # noqa: BLE001 - offline/no Jira: still pause the gate
                comment_url, baseline = "", 0
                run_log(run_id, "JIRA", "[STUB] Jira unavailable; pausing gate offline.")
            approval.pause_plan_gate(run_id, store, comment_url, comments_baseline=baseline)
            store.update_data(run_id, plan=plan)
        except Exception as e:  # noqa: BLE001
            to_failed_rail(run_id, store, e)

    def on_decision(self, run_id: str, decision: approval.CodingDecision) -> None:
        store = self.d.store
        result = approval.resume_plan_gate(run_id, store, decision)
        if result.resumed_to == CodingState.PLANNING.value:
            # request_changes: re-pause at the gate (a real agent would re-plan).
            rec = store.load(run_id)
            store.transition(run_id, CodingState.AWAITING_APPROVAL)
            approval.pause_plan_gate(run_id, store, "", comments_baseline=0)
            run_log(run_id, "PLANNER", "[STUB] re-planned; awaiting approval again.")
            return
        rec = store.load(run_id)
        ticket_key = rec.ticket or ""
        try:
            run_log(run_id, "CODER", "[STUB] implementing (no real change).")
            store.transition(run_id, CodingState.CREATING_PR)
            pr_url = self.d.bitbucket.open_pull_request(
                repo="example-repo", source_branch=f"auto/{ticket_key}",
                title=f"[{ticket_key}] automated change")
            store.update_data(run_id, pr_url=pr_url)
            store.transition(run_id, CodingState.DONE)
            run_log(run_id, "DONE", f"[STUB] coding run complete. PR: {pr_url}")
        except Exception as e:  # noqa: BLE001
            to_failed_rail(run_id, store, e)
