"""Azure Spot VM client — REFERENCE STUB (Layer A).

Provisions and tears down the ephemeral Spot VM the regression workflow runs its
load tests on. This shipped stub is a safe no-op so the host boots and the
UI-test pipeline works out of the box; wire up the real Azure calls (azure-mgmt-
compute / the az CLI) where marked before using the regression workflow.
"""
from __future__ import annotations

from ..config import Settings
from ..utils.logging import get_logger

_log = get_logger("azure")


class AzureSpotClient:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def provision(self) -> str:
        """Create a Spot VM and return its name. STUB: returns a fake name.
        TODO: replace with a real azure-mgmt-compute deployment."""
        name = f"{self.s.azure_vm_name_prefix}-stub"
        _log.info("[STUB] provision Spot VM %s (no real Azure call made)", name)
        return name

    def teardown(self, vm_name: str) -> None:
        """Delete the VM. STUB: no-op. MUST be real before production — a live
        implementation is what stops orphaned Spot VMs from accruing cost.
        TODO: replace with a real delete of the VM + its disk/NIC."""
        _log.info("[STUB] teardown Spot VM %s (no real Azure call made)", vm_name)
