"""Bitbucket client — REFERENCE STUB (Layer A).

Opens the pull request for a completed run. This stub returns a browse URL so the
UI-test workflow completes cleanly without Bitbucket credentials; wire up the
real Bitbucket Cloud REST call where marked to open actual PRs.
"""
from __future__ import annotations

from ..config import Settings
from ..utils.logging import get_logger

_log = get_logger("bitbucket")


class BitbucketClient:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def open_pull_request(self, repo: str, source_branch: str,
                          title: str, description: str = "") -> str:
        """Open a PR and return its URL. STUB: returns a placeholder URL.
        TODO: replace with a real POST to
        /2.0/repositories/{workspace}/{repo}/pullrequests."""
        url = f"https://bitbucket.org/{self.s.bitbucket_workspace}/{repo}/pull-requests"
        _log.info("[STUB] open PR on %s from %s (no real Bitbucket call made)",
                  repo, source_branch)
        return url
