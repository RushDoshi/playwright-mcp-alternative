"""FastAPI entry point (Layer A).

Endpoints:
  POST /webhook/jira      — Jira "Ready for Development" transition -> start coding workflow
  POST /run/uitest        — generate Playwright UI tests for a ticket
  POST /uitest/{run}/decision    — UI-test gate resume (approve | request_changes)
  POST /run/regression    — manual kick-off of the regression workflow
  POST /coding/{run}/decision    — Gate 1 resume (approve | request_changes)
  POST /regression/{run}/decision— Gate 2 resume (approve | skip)
  GET  /health

The host owns secrets and spawns workflows as background tasks so the HTTP call returns
immediately (the run continues and pauses itself at its gate).
"""
from __future__ import annotations

import asyncio
import threading
import uuid
from pathlib import Path

import secrets as _secrets
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .clients.azure_spot_client import AzureSpotClient
from .clients.bitbucket_client import BitbucketClient
from .clients.claude_runner import ClaudeRunner
from .clients.email_client import EmailClient
from .clients.jenkins_client import JenkinsClient
from .clients.jira_client import JiraClient
from .config import get_settings
from .gates import approval
from .gates.jira_poller import JiraApprovalPoller, JiraReadyPoller
from .orchestrator.coding_workflow import CodingDeps, CodingWorkflow
from .orchestrator.regression_workflow import RegressionDeps, RegressionWorkflow
from .orchestrator.uitest_workflow import UITestDeps, UITestWorkflow
from .state import StateStore
from .utils.logging import configure, get_logger, get_run_logs

_poller_task: asyncio.Task | None = None
_ready_poller_task: asyncio.Task | None = None


def _start_pipeline_for_ticket(key: str) -> dict:
    """Mint run ids and start the Coding Agent (and, when enabled, the UI-Test
    Agent) for a ticket now in Ready for Development — shared by the
    Jira-push webhook and the pull-based ``JiraReadyPoller`` so both triggers
    go through identical run-minting logic."""
    settings = get_settings()
    store = _store()
    run_id = f"code-{uuid.uuid4().hex[:8]}"
    store.create(run_id, "coding", ticket=key)
    store.supersede_open_runs_for_ticket(key, except_run_id=run_id, prefix="code-")
    threading.Thread(target=_coding_workflow().start, args=(run_id, key), daemon=True).start()
    result = {"run_id": run_id, "ticket": key, "status": "PENDING"}

    if settings.auto_start_uitest_on_ready:
        uitest_run_id = f"ui-{uuid.uuid4().hex[:8]}"
        store.create(uitest_run_id, "coding", ticket=key)  # same state machine
        store.supersede_open_runs_for_ticket(key, except_run_id=uitest_run_id, prefix="ui-")
        threading.Thread(target=_uitest_workflow().start, args=(uitest_run_id, key, None),
                         daemon=True).start()
        result["uitest_run_id"] = uitest_run_id

    return result


def _auto_resume(run_id: str) -> None:
    """Gate-1 auto-approve callback for the Jira poller — routes to whichever
    workflow owns the run (by the run_id prefix, same convention the manual
    /decision endpoints use) and runs it off-thread so a long Playwright run
    never blocks the poller or the HTTP server."""
    def _do() -> None:
        try:
            if run_id.startswith("ui-"):
                _uitest_workflow().on_decision(run_id, approval.CodingDecision.APPROVE)
            elif run_id.startswith("code-"):
                _coding_workflow().on_decision(run_id, approval.CodingDecision.APPROVE)
        except Exception:  # noqa: BLE001
            get_logger("main").exception(f"auto-approve failed for run {run_id}")
    threading.Thread(target=_do, daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _poller_task, _ready_poller_task
    s = get_settings()
    configure(s.log_level)
    if not s.webhook_secret:
        get_logger("main").warning(
            "WEBHOOK_SECRET is not set — POST endpoints are UNAUTHENTICATED. "
            "Set it before exposing this service beyond localhost.")

    if s.jira_poll_interval_seconds > 0 and s.jira_base_url:
        poller = JiraApprovalPoller(_store(), JiraClient(s), _auto_resume,
                                    interval_seconds=s.jira_poll_interval_seconds,
                                    max_concurrent_checks=s.jira_poll_max_concurrent)
        _poller_task = asyncio.create_task(poller.run_forever())
    else:
        poller = None
        get_logger("main").warning(
            "Jira approval polling is disabled (no JIRA_BASE_URL or "
            "JIRA_POLL_INTERVAL_SECONDS=0) — Gate 1 only resumes via the manual "
            "/decision endpoint.")

    if s.jira_ready_poll_interval_seconds > 0 and s.jira_ready_jql and s.jira_base_url:
        ready_poller = JiraReadyPoller(
            _store(), JiraClient(s), s.jira_ready_jql,
            lambda key: threading.Thread(target=_start_pipeline_for_ticket, args=(key,),
                                         daemon=True).start(),
            interval_seconds=s.jira_ready_poll_interval_seconds)
        _ready_poller_task = asyncio.create_task(ready_poller.run_forever())
    else:
        ready_poller = None
        get_logger("main").info(
            "Jira ready-poller is disabled (set JIRA_READY_JQL and "
            "JIRA_READY_POLL_INTERVAL_SECONDS to enable a pull-based trigger "
            "when no Jira Automation rule POSTs to /webhook/jira).")

    yield

    if poller is not None and _poller_task is not None:
        poller.stop()
        _poller_task.cancel()
        try:
            await _poller_task
        except asyncio.CancelledError:
            pass

    if ready_poller is not None and _ready_poller_task is not None:
        ready_poller.stop()
        _ready_poller_task.cancel()
        try:
            await _ready_poller_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="AI-Native Quality OS", version="0.1.0", lifespan=lifespan)

# demo/AI_Quality_OS_Demo.html drives this API from a local file:// or
# localhost-served page — either way that's a different origin than this API,
# so the browser blocks its fetch() calls without CORS allowed. Same-machine
# demo use only; the webhook-token check (verify_token, below) is the real
# access control, not this.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# Serves each ticket's Playwright html-report/ and allure-report/ over real
# HTTP (not file://) at /reports/<ticket>/... — the demo page links here once
# a run finishes. Real HTTP matters for the Allure report specifically: it
# loads its data via fetch(), which browsers block from a file:// origin.
_PLAYWRIGHT_RESULTS_DIR = Path(__file__).resolve().parents[2] / "playwright" / "results"
_PLAYWRIGHT_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/reports", StaticFiles(directory=str(_PLAYWRIGHT_RESULTS_DIR)), name="reports")


def verify_token(x_webhook_token: str | None = Header(default=None)) -> None:
    """Shared-secret check on all mutating endpoints.

    Enforced whenever WEBHOOK_SECRET is configured; constant-time comparison. If the
    secret is unset (local dev), requests pass but a warning is logged at startup.
    """
    secret = get_settings().webhook_secret
    if not secret:
        return
    if not x_webhook_token or not _secrets.compare_digest(x_webhook_token, secret):
        raise HTTPException(status_code=401, detail="invalid or missing X-Webhook-Token")


def _store() -> StateStore:
    return StateStore(get_settings().state_dir)


def _coding_workflow() -> CodingWorkflow:
    s = get_settings()
    return CodingWorkflow(CodingDeps(
        settings=s, store=_store(), jira=JiraClient(s),
        bitbucket=BitbucketClient(s), claude=ClaudeRunner(s)))


def _uitest_workflow() -> UITestWorkflow:
    s = get_settings()
    return UITestWorkflow(UITestDeps(
        settings=s, store=_store(), jira=JiraClient(s), claude=ClaudeRunner(s)))


def _regression_workflow() -> RegressionWorkflow:
    s = get_settings()
    return RegressionWorkflow(RegressionDeps(
        settings=s, store=_store(), azure=AzureSpotClient(s),
        jenkins=JenkinsClient(s), email=EmailClient(s)))


# ---- models ----
class CodingDecisionBody(BaseModel):
    decision: str  # "approve" | "request_changes"


class RerunDecisionBody(BaseModel):
    decision: str  # "approve" | "skip"


class UITestStartBody(BaseModel):
    ticket: str
    env: str | None = None            # overrides TEST_ENV for this run


class UITestDecisionBody(BaseModel):
    decision: str                     # "approve" | "request_changes"
    note: str = ""                    # free-text revision request


# ---- coding ----
@app.post("/webhook/jira", dependencies=[Depends(verify_token)])
def jira_webhook(payload: dict) -> dict:
    """Triggered when a ticket transitions to Ready for Development.

    Generic for every ticket. Starts the Coding Agent, and — when
    AUTO_START_UITEST_ON_READY is enabled (default on) — also starts the UI-Test
    Agent, so this one trigger drives the full Jira -> plan -> approval ->
    Playwright -> HTML-report pipeline without a separate manual call.

    Requires a Jira Automation rule configured to POST here on the status
    change. Hosts without that configured can instead enable
    ``JiraReadyPoller`` (JIRA_READY_JQL + JIRA_READY_POLL_INTERVAL_SECONDS),
    which reaches the same ``_start_pipeline_for_ticket`` pull-based.
    """
    settings = get_settings()
    issue = payload.get("issue", {})
    key = issue.get("key")
    status = issue.get("fields", {}).get("status", {}).get("name", "")
    if not key:
        raise HTTPException(status_code=400, detail="missing issue key")
    if status and status != settings.jira_ready_status:
        return {"ignored": True, "reason": f"status '{status}' is not '{settings.jira_ready_status}'"}

    return _start_pipeline_for_ticket(key)


@app.post("/coding/{run_id}/decision", dependencies=[Depends(verify_token)])
def coding_decision(run_id: str, body: CodingDecisionBody, background: BackgroundTasks) -> dict:
    try:
        decision = approval.CodingDecision(body.decision)
    except ValueError:
        raise HTTPException(status_code=400, detail="decision must be approve|request_changes")
    background.add_task(_coding_workflow().on_decision, run_id, decision)
    return {"run_id": run_id, "accepted": body.decision}


# ---- UI tests (Playwright) ----
@app.post("/run/uitest", dependencies=[Depends(verify_token)])
def run_uitest(body: UITestStartBody, background: BackgroundTasks) -> dict:
    """Generate Playwright tests for a ticket (same gate as the coding workflow)."""
    key = body.ticket.strip().upper()
    if not key:
        raise HTTPException(status_code=400, detail="missing ticket")
    run_id = f"ui-{uuid.uuid4().hex[:8]}"
    store = _store()
    store.create(run_id, "coding", ticket=key)   # same state machine
    store.supersede_open_runs_for_ticket(key, except_run_id=run_id, prefix="ui-")
    background.add_task(_uitest_workflow().start, run_id, key, body.env)
    return {"run_id": run_id, "ticket": key, "status": "PENDING"}


@app.post("/uitest/{run_id}/decision", dependencies=[Depends(verify_token)])
def uitest_decision(run_id: str, body: UITestDecisionBody,
                    background: BackgroundTasks) -> dict:
    try:
        decision = approval.CodingDecision(body.decision)
    except ValueError:
        raise HTTPException(status_code=400,
                            detail="decision must be approve|request_changes")
    background.add_task(_uitest_workflow().on_decision, run_id, decision, body.note)
    return {"run_id": run_id, "accepted": body.decision}


# ---- regression ----
@app.post("/run/regression", dependencies=[Depends(verify_token)])
def run_regression(background: BackgroundTasks) -> dict:
    run_id = f"reg-{uuid.uuid4().hex[:8]}"
    _store().create(run_id, "regression")
    background.add_task(_regression_workflow().start, run_id)
    return {"run_id": run_id, "status": "QUEUED"}


@app.post("/regression/{run_id}/decision", dependencies=[Depends(verify_token)])
def regression_decision(run_id: str, body: RerunDecisionBody, background: BackgroundTasks) -> dict:
    try:
        decision = approval.RerunDecision(body.decision)
    except ValueError:
        raise HTTPException(status_code=400, detail="decision must be approve|skip")
    background.add_task(_regression_workflow().on_decision, run_id, decision)
    return {"run_id": run_id, "accepted": body.decision}


# ---- status / health ----
@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    try:
        rec = _store().load(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run_id": rec.run_id, "workflow": rec.workflow, "status": rec.status,
            "ticket": rec.ticket, "awaiting": rec.awaiting, "error": rec.error}


@app.get("/runs/{run_id}/logs")
def get_run_log_lines(run_id: str, since: int = 0) -> dict:
    """Incremental log feed for a UI. ``since`` is the count already displayed."""
    lines = get_run_logs(run_id, since)
    return {"run_id": run_id, "since": since, "count": len(lines), "logs": lines}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "host": get_settings().host_name}
