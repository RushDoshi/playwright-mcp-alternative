"""Jenkins client — REFERENCE STUB (Layer A).

Triggers the regression job and reads its result artifacts. This stub returns
canned "success" values so the regression workflow runs without a Jenkins
server; wire up the real Jenkins REST calls where marked.
"""
from __future__ import annotations

from ..config import Settings
from ..utils.logging import get_logger

_log = get_logger("jenkins")


class JenkinsClient:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def trigger_job(self, params: dict | None = None) -> int:
        """Start a build; return its build number. STUB: returns 0.
        TODO: POST to {JENKINS_URL}/job/{job}/buildWithParameters and poll the
        queue item for the real build number."""
        _log.info("[STUB] trigger Jenkins job %s (no real call made)",
                  self.s.jenkins_regression_job)
        return 0

    def wait_for_result(self, build_number: int, timeout_s: int = 3600) -> str:
        """Block until the build finishes; return 'SUCCESS'|'FAILURE'|'ABORTED'.
        STUB: returns 'SUCCESS'. TODO: poll {job}/{build}/api/json for `result`."""
        _log.info("[STUB] wait for Jenkins build %s (returns SUCCESS)", build_number)
        return "SUCCESS"

    def fetch_junit(self, build_number: int) -> list[str]:
        """Return local paths to the build's JUnit XML. STUB: returns []."""
        _log.info("[STUB] fetch JUnit for build %s (returns none)", build_number)
        return []
