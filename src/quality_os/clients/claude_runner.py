"""Claude Code runner (Layer A).

Invokes a named Layer-B agent via the Claude Code CLI as a subprocess (`claude -p`),
with the host-provided model and a scoped environment. This is the only bridge from the
deterministic runtime into the reasoning layer. 30-minute timeout per the prototype.

Auth: this project runs on the host's Claude Pro/Max subscription login (`claude
login`), never a metered Anthropic API key. ``ANTHROPIC_API_KEY`` is therefore only
forwarded into the subprocess when it holds a real key; an unfilled placeholder
(".env.example"'s own "changeme", "paste-...", or empty) is stripped instead of
passed through, so the CLI falls back to its own logged-in subscription session
rather than attempting (and failing) key-based auth.

Persona loading: the CLI's own ``--agent <name>`` flag only selects among its small,
fixed set of built-in session agent *types* (``general-purpose``, ``Explore``,
``Plan``, ...) -- confirmed live against the installed CLI (2.1.267), which rejected
a project agent path with "--agent 'coding/ui-test-designer' not found. Available
agents: claude, Explore, general-purpose, Plan, statusline-setup". There is no CLI
mechanism to load a project-defined persona file by path via ``--agent``. So this
reads ``.claude/agents/<agent>.md`` itself and feeds its contents in as the one-shot
system prompt via ``--system-prompt``; ``--output-format json`` makes the response
machine-parseable (the ``result`` field is the agent's final answer).
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..utils.logging import get_logger

_log = get_logger("claude")

# repo root = .../src/quality_os/clients/claude_runner.py -> parents[3]
ROOT = Path(__file__).resolve().parents[3]
AGENTS_DIR = ROOT / ".claude" / "agents"


def _is_real_api_key(key: str) -> bool:
    return bool(key) and key != "changeme" and not key.startswith("paste-")


@dataclass
class AgentResult:
    ok: bool
    output: str
    error: str = ""


class ClaudeRunner:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def run_agent(self, agent: str, prompt: str, cwd: str | None = None,
                  extra_env: dict[str, str] | None = None) -> AgentResult:
        """Run a named agent (e.g. 'coding/test-designer') against a prompt.

        The agent file under .claude/agents/<agent>.md defines the persona; the host
        passes the model and any per-call env (e.g. JAVA_HOME). External calls in tests
        are mocked by patching this method.
        """
        agent_file = AGENTS_DIR / f"{agent}.md"
        try:
            persona = agent_file.read_text(encoding="utf-8")
        except OSError as e:
            return AgentResult(ok=False, output="",
                              error=f"agent persona file not found: {agent_file} ({e})")

        env = {**os.environ, "CLAUDE_MODEL": self.s.claude_model, **(extra_env or {})}
        if _is_real_api_key(self.s.anthropic_api_key):
            env["ANTHROPIC_API_KEY"] = self.s.anthropic_api_key
        else:
            env.pop("ANTHROPIC_API_KEY", None)

        cmd = ["claude", "-p", "--system-prompt", persona, "--output-format", "json",
               "--model", self.s.claude_model, prompt]
        _log.info("claude -p --system-prompt <%s.md> (model=%s)", agent, self.s.claude_model)
        try:
            res = subprocess.run(
                cmd, cwd=cwd, env=env, capture_output=True, text=True,
                timeout=self.s.claude_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return AgentResult(ok=False, output="", error="claude -p timed out (30 min)")

        if res.returncode != 0:
            return AgentResult(ok=False, output=res.stdout, error=res.stderr.strip())

        try:
            data = json.loads(res.stdout)
        except json.JSONDecodeError as e:
            return AgentResult(ok=False, output=res.stdout,
                              error=f"unparsable claude -p --output-format json output: {e}")

        result = str(data.get("result", ""))
        if data.get("is_error"):
            return AgentResult(ok=False, output=result, error=result or "claude -p reported an error")
        return AgentResult(ok=True, output=result.strip())
