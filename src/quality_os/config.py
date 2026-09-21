"""Central configuration for the Quality OS runtime (Layer A).

All secrets live only on the host and are loaded from the environment. Agents never
carry their own credentials — the host passes scoped values to ``claude -p`` per call.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Host ---
    host_name: str = Field(default="host-vm-qa-01", alias="HOST_NAME")
    workspace_dir: str = Field(default="/app/workspace", alias="WORKSPACE_DIR")
    state_dir: str = Field(default="/app/state", alias="STATE_DIR")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # --- Security ---
    webhook_secret: str = Field(default="", alias="WEBHOOK_SECRET")

    # --- Jira ---
    jira_base_url: str = Field(default="", alias="JIRA_BASE_URL")
    jira_email: str = Field(default="", alias="JIRA_EMAIL")
    jira_api_token: str = Field(default="", alias="JIRA_API_TOKEN")
    jira_ready_status: str = Field(default="Ready for Development", alias="JIRA_READY_STATUS")
    jira_inprogress_status: str = Field(default="In Progress", alias="JIRA_INPROGRESS_STATUS")
    jira_inreview_status: str = Field(default="In Review", alias="JIRA_INREVIEW_STATUS")
    # Auto-start the UI-Test (Playwright) Agent on the same "Ready for Development"
    # webhook that starts the Coding Agent — makes the Jira->Playwright pipeline
    # fully automatic for every ticket, not just tickets triggered manually via /run/uitest.
    auto_start_uitest_on_ready: bool = Field(default=True, alias="AUTO_START_UITEST_ON_READY")
    # Gate 1 auto-resume: how often (seconds) the host polls Jira for a reply comment
    # that is exactly the word "Approved" on tickets parked at AWAITING_APPROVAL.
    # 0 disables the poller (fall back to the manual /decision endpoint only).
    jira_poll_interval_seconds: int = Field(default=30, alias="JIRA_POLL_INTERVAL_SECONDS")
    # How many Gate-1-paused tickets the poller checks against Jira at once, per
    # poll cycle. Checking them one at a time (the old behavior) means N paused
    # tickets take N sequential Jira round-trips before the last one is ever
    # looked at in a single cycle — at hundreds/thousands of tickets parked at
    # once, that alone can exceed jira_poll_interval_seconds, so the poller falls
    # permanently behind its own schedule. Bounded (not unlimited) so this still
    # can't hammer Jira's API with unbounded concurrent requests.
    jira_poll_max_concurrent: int = Field(default=10, alias="JIRA_POLL_MAX_CONCURRENT")
    # Pull-based trigger alternative to /webhook/jira, for when no Jira Automation
    # rule POSTs to this host. Disabled by default on BOTH settings — must be
    # explicitly scoped (e.g. "project = PROJ") so it never sweeps every
    # "Ready for Development" ticket in the whole Jira instance.
    jira_ready_jql: str = Field(default="", alias="JIRA_READY_JQL")
    jira_ready_poll_interval_seconds: int = Field(default=0, alias="JIRA_READY_POLL_INTERVAL_SECONDS")

    # --- UI tests (Playwright) ---
    test_env: str = Field(default="", alias="TEST_ENV")
    test_user: str = Field(default="", alias="TEST_USER")
    test_pass: str = Field(default="", alias="TEST_PASS")
    ticket_key: str = Field(default="", alias="TICKET_KEY")
    # Local/demo only — a real Chrome window instead of headless, so a human can
    # watch the run live. Leave false on the host (host-vm-qa-01 stays headless).
    playwright_headed: bool = Field(default=False, alias="PLAYWRIGHT_HEADED")

    # --- Bitbucket ---
    bitbucket_workspace: str = Field(default="", alias="BITBUCKET_WORKSPACE")
    bitbucket_user: str = Field(default="", alias="BITBUCKET_USER")
    bitbucket_app_password: str = Field(default="", alias="BITBUCKET_APP_PASSWORD")

    # --- Anthropic / Claude Code ---
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    claude_model: str = Field(default="claude-sonnet-4-6", alias="CLAUDE_MODEL")
    claude_timeout_seconds: int = Field(default=1800, alias="CLAUDE_TIMEOUT_SECONDS")

    # --- Jenkins ---
    jenkins_url: str = Field(default="", alias="JENKINS_URL")
    jenkins_user: str = Field(default="", alias="JENKINS_USER")
    jenkins_api_token: str = Field(default="", alias="JENKINS_API_TOKEN")
    jenkins_regression_job: str = Field(default="LoadTesting/Regression", alias="JENKINS_REGRESSION_JOB")

    # --- Azure Spot VM ---
    azure_subscription_id: str = Field(default="", alias="AZURE_SUBSCRIPTION_ID")
    azure_resource_group: str = Field(default="qa-regression-rg", alias="AZURE_RESOURCE_GROUP")
    azure_vm_size: str = Field(default="Standard_D4s_v5", alias="AZURE_VM_SIZE")
    azure_vm_name_prefix: str = Field(default="vm-qa-spot", alias="AZURE_VM_NAME_PREFIX")
    azure_location: str = Field(default="eastus", alias="AZURE_LOCATION")

    # --- SMTP / Email ---
    smtp_host: str = Field(default="", alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_user: str = Field(default="", alias="SMTP_USER")
    smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    report_from: str = Field(default="qa-bot@example.com", alias="REPORT_FROM")
    report_to: str = Field(default="qa-team@example.com", alias="REPORT_TO")


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
