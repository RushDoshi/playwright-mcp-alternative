"""Regression (load-test) workflow driver — REFERENCE STUB (Layer A).

The production system provisions an Azure Spot VM, runs a JMeter/Playwright load
regression via Jenkins, analyses failures, pauses at a second human gate
(heal + rerun vs report as-is), then emails a report and always tears the VM
down. That analysis engine is not part of this open-source release.

This stub drives the SAME state machine and the SAME Gate-2 rerun flow so the
host boots, every /regression endpoint works, runs reach a terminal state, and
the VM-teardown cleanup path is exercised. It runs no real load test. Replace the
marked bodies with a real implementation; keep the teardown in the FAILED rail.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..clients.azure_spot_client import AzureSpotClient
from ..clients.email_client import EmailClient
from ..clients.jenkins_client import JenkinsClient
from ..config import Settings
from ..gates import approval
from ..state import RegressionState, StateStore
from ..utils.errors import to_failed_rail
from ..utils.logging import run_log


@dataclass
class RegressionDeps:
    settings: Settings
    store: StateStore
    azure: AzureSpotClient
    jenkins: JenkinsClient
    email: EmailClient


class RegressionWorkflow:
    """QUEUED -> PROVISIONING -> RUNNING -> ANALYZING
             -> AWAITING_RERUN_APPROVAL (Gate 2) -> RERUNNING? -> REPORTING
             -> COMPLETE   (+ FAILED rail, VM ALWAYS torn down)."""

    def __init__(self, deps: RegressionDeps) -> None:
        self.d = deps

    def start(self, run_id: str) -> None:
        store = self.d.store
        vm_name = ""
        try:
            store.transition(run_id, RegressionState.PROVISIONING)
            vm_name = self.d.azure.provision()
            store.update_data(run_id, vm_name=vm_name)
            run_log(run_id, "AZURE", f"[STUB] provisioned {vm_name}")

            store.transition(run_id, RegressionState.RUNNING)
            build = self.d.jenkins.trigger_job()
            self.d.jenkins.wait_for_result(build)
            run_log(run_id, "JENKINS", "[STUB] regression run finished.")

            store.transition(run_id, RegressionState.ANALYZING)
            # A real analyser classifies failures into flaky (healable) vs genuine.
            healable, genuine = 0, 0
            run_log(run_id, "ANALYST",
                    f"[STUB] {healable} healable, {genuine} genuine failures.")

            store.transition(run_id, RegressionState.AWAITING_RERUN_APPROVAL)
            approval.pause_rerun_gate(run_id, store, healable, genuine)
        except Exception as e:  # noqa: BLE001
            # VM teardown MUST run even on failure.
            to_failed_rail(run_id, store, e,
                           cleanup=lambda: self.d.azure.teardown(vm_name) if vm_name else None)

    def on_decision(self, run_id: str, decision: approval.RerunDecision) -> None:
        store = self.d.store
        vm_name = store.load(run_id).data.get("vm_name", "")
        try:
            result = approval.resume_rerun_gate(run_id, store, decision)
            if result.resumed_to == RegressionState.RERUNNING.value:
                run_log(run_id, "RERUN", "[STUB] re-running healed tests.")
                store.transition(run_id, RegressionState.REPORTING)
            # else: skip -> already at REPORTING via resume_rerun_gate.
            self.d.email.send_report(
                subject=f"Regression report — run {run_id}",
                body="[STUB] regression report (no real load test was run).")
            store.transition(run_id, RegressionState.COMPLETE)
            run_log(run_id, "DONE", "[STUB] regression run complete.")
        except Exception as e:  # noqa: BLE001
            to_failed_rail(run_id, store, e,
                           cleanup=lambda: self.d.azure.teardown(vm_name) if vm_name else None)
        finally:
            # Teardown on the success path too — real Spot VMs cost money idle.
            if vm_name:
                self.d.azure.teardown(vm_name)
