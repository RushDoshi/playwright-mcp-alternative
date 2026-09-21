"""UI-test (Playwright) workflow driver — Layer A.

Same state machine and same human gate as the Coding workflow::

  PENDING -> CLONING -> PLANNING -> AWAITING_APPROVAL (Gate 1, in Jira)
          -> IMPLEMENTING -> CREATING_PR -> DONE      (+ FAILED rail)

Difference from :mod:`coding_workflow`: instead of asking an agent to write source
code, this driver

1. **discovers** the page deterministically with Playwright (no LLM tokens),
2. asks Sonnet for a **closed action-DSL** (never raw code), and
3. **renders** the ``.spec.ts`` itself, then runs deterministic gates.

That split is what keeps generated tests syntactically valid by construction: the
model chooses *what* to test, the host decides *how* it is written.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..clients.claude_runner import ClaudeRunner
from ..clients.jira_client import JiraClient
from ..config import Settings
from ..domain import junit_parser
from ..gates import approval
from ..state import CodingState, StateStore
from ..uitest import discovery as disco
from ..uitest.designer import design_scenarios
from ..uitest.envs import load_ui_config
from ..uitest.generator import gates as render_gates
from ..uitest.generator import normalize_progressive_login, render_spec
from ..utils.errors import to_failed_rail
from ..utils.logging import run_log

# Fallback selectors: only used when discovery cannot reach the page at all, so a
# run degrades instead of dying (kept out of the renderer, which stays generic).
_FALLBACK_SELECTORS = {
    "iframe": None, "email": "#username", "password": "#password",
    "submit": "button[type='submit']",
}


@dataclass
class UITestDeps:
    settings: Settings
    store: StateStore
    jira: JiraClient
    # optional: without one, design_scenarios() always uses the built-in fallback
    # (login-only) scenarios instead of shelling out to `claude -p`
    claude: ClaudeRunner | None = None
    # optional so tests can inject a fake; defaults to the real Playwright runner
    run_tests: object | None = None


class UITestWorkflow:
    """Ticket -> discovered selectors -> designed scenarios -> spec -> PR."""

    def __init__(self, deps: UITestDeps) -> None:
        self.d = deps

    # ---- entry: PENDING -> ... -> AWAITING_APPROVAL (then pause) ----
    def start(self, run_id: str, ticket_key: str, env_name: str | None = None) -> None:
        store = self.d.store
        try:
            run_log(run_id, "JIRA", f"Ticket {ticket_key} -> Ready for Development.")
            cfg = load_ui_config(self.d.settings, ticket=ticket_key, env_name=env_name)
            run_log(run_id, "CONFIG",
                    f"environment '{cfg['env_name']}' -> {cfg['login_url']}")

            requirement = self._requirement(run_id, ticket_key)

            store.transition(run_id, CodingState.CLONING)
            run_log(run_id, "WORKSPACE", f"workspace ready for {ticket_key}")

            store.transition(run_id, CodingState.PLANNING)
            selectors, page_summary = self._discover(run_id, cfg)
            scenarios, source = design_scenarios(
                cfg, requirement, page_summary,
                log=lambda m: run_log(run_id, "LLM", m), claude=self.d.claude)
            scenarios = normalize_progressive_login(scenarios, selectors, cfg)
            run_log(run_id, "DESIGNER",
                    f"{len(scenarios)} scenarios ({source}): "
                    + "; ".join(s["title"] for s in scenarios))

            plan = self._plan_markdown(ticket_key, cfg, selectors, scenarios, source)

            store.transition(run_id, CodingState.AWAITING_APPROVAL)
            comment_url = self.d.jira.post_plan_comment(ticket_key, plan)
            baseline = self.d.jira.comment_count(ticket_key)
            approval.pause_plan_gate(run_id, store, comment_url, comments_baseline=baseline)
            store.update_data(run_id, env_name=cfg["env_name"], plan=plan,
                              requirement=requirement, page_summary=page_summary,
                              selectors=selectors, scenarios=scenarios, source=source)
        except Exception as e:  # noqa: BLE001
            to_failed_rail(run_id, store, e)
            self._safe_jira_error(ticket_key, e)

    # ---- resume on the human decision in Jira ----
    def on_decision(self, run_id: str, decision: approval.CodingDecision,
                    note: str = "") -> None:
        store = self.d.store
        approval.resume_plan_gate(run_id, store, decision)
        rec = store.load(run_id)
        ticket_key = rec.ticket or ""

        if decision is approval.CodingDecision.REQUEST_CHANGES:
            try:
                cfg = load_ui_config(self.d.settings, ticket=ticket_key,
                                     env_name=rec.data.get("env_name"))
                run_log(run_id, "DESIGNER", f"Re-designing from reviewer note: {note!r}")
                selectors = rec.data.get("selectors", {})
                scenarios, source = design_scenarios(
                    cfg, rec.data.get("requirement", ""), rec.data.get("page_summary", ""),
                    log=lambda m: run_log(run_id, "LLM", m), extra_note=note,
                    claude=self.d.claude)
                scenarios = normalize_progressive_login(scenarios, selectors, cfg)
                plan = self._plan_markdown(ticket_key, cfg, selectors,
                                           scenarios, source, note=note)
                store.transition(run_id, CodingState.AWAITING_APPROVAL)
                comment_url = self.d.jira.post_plan_comment(ticket_key, plan)
                baseline = self.d.jira.comment_count(ticket_key)
                approval.pause_plan_gate(run_id, store, comment_url, comments_baseline=baseline)
                store.update_data(run_id, plan=plan, scenarios=scenarios, source=source)
            except Exception as e:  # noqa: BLE001
                to_failed_rail(run_id, store, e)
            return

        self._implement(run_id)

    # ---- IMPLEMENTING -> CREATING_PR -> DONE ----
    def _implement(self, run_id: str) -> None:
        store = self.d.store
        rec = store.load(run_id)
        ticket_key = rec.ticket or ""
        try:
            cfg = load_ui_config(self.d.settings, ticket=ticket_key,
                                 env_name=rec.data.get("env_name"))
            scenarios = rec.data.get("scenarios") or []
            selectors = rec.data.get("selectors") or _FALLBACK_SELECTORS

            # NB: approval.resume_plan_gate() has already moved the run into
            # IMPLEMENTING; transitioning again would violate the state guard.
            self._safe_transition(ticket_key, self.d.settings.jira_inprogress_status)

            attachments = self._stage_attachments(run_id, ticket_key, scenarios)

            spec_path = self._spec_path(ticket_key)
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            if spec_path.exists():
                spec_path.unlink()          # never run a stale spec
            spec_path.write_text(
                render_spec(cfg, selectors, scenarios, ticket_key, attachments=attachments),
                encoding="utf-8")
            run_log(run_id, "WRITER", f"rendered {spec_path.name} "
                                      f"({len(scenarios)} scenarios)")

            failures = render_gates(spec_path, expected_tests=len(scenarios))
            if failures:
                raise RuntimeError("generation gates failed: " + "; ".join(failures))
            run_log(run_id, "GATE", "generation gates passed")

            ok, summary = self._run_playwright(run_id, ticket_key, cfg)
            run_log(run_id, "RUNNER", summary)
            # Generated regardless of pass/fail (same as ci/Jenkinsfile's `post
            # always`) — a red run still needs a report to diagnose from. Skipped
            # under the injected test runner (tests/CI): no real Playwright run
            # happened, so there's nothing real to render a report from.
            if self.d.run_tests is None:
                self._generate_allure_report(run_id, ticket_key)
            if not ok:
                raise RuntimeError("Playwright run failed — not opening a PR")

            store.transition(run_id, CodingState.CREATING_PR)
            pr_url = (f"{self.d.settings.jira_base_url.rstrip('/')}"
                      f"/browse/{ticket_key}")  # replaced by BitbucketClient in CI
            self._safe_comment(ticket_key,
                               f"Implementation complete for {ticket_key}\n\n"
                               f"Spec: {self._spec_relative(ticket_key)}\n"
                               f"Scenarios: {len(scenarios)}\n\n"
                               f"Result: {summary}\n\n"
                               f"HTML report: {self._html_report_path(ticket_key)}\n"
                               f"Allure report: {self._allure_report_path(ticket_key)}")
            self._safe_transition(ticket_key, self.d.settings.jira_inreview_status)

            store.update_data(run_id, pr_url=pr_url, spec_path=str(spec_path))
            store.transition(run_id, CodingState.DONE)
            run_log(run_id, "DONE", "UI test task complete.")
        except Exception as e:  # noqa: BLE001
            to_failed_rail(run_id, store, e)
            self._safe_jira_error(ticket_key, e)

    # ---------------- helpers ----------------
    def _requirement(self, run_id: str, ticket_key: str) -> str:
        try:
            ticket = self.d.jira.fetch_ticket(ticket_key)
            run_log(run_id, "ANALYST", "Read description + comments bottom-to-top.")
            return ticket.requirement_text()
        except Exception as e:  # noqa: BLE001
            run_log(run_id, "ANALYST", f"Jira unavailable ({e}); continuing offline.")
            return f"TICKET {ticket_key}: UI automation (ticket text unavailable)"

    def _discover(self, run_id: str, cfg: dict) -> tuple[dict, str]:
        try:
            page_map = disco.discover(cfg["login_url"],
                                      log=lambda m: run_log(run_id, "DISCOVER", m))
            selectors = disco.pick_login_selectors(page_map)
            if not selectors:
                raise disco.DiscoveryError("no login fields recognised on the page")
            run_log(run_id, "DISCOVER",
                    f"selectors: iframe={selectors['iframe']} email={selectors['email']} "
                    f"password={selectors['password']} submit={selectors['submit']}")
            return selectors, disco.compact_map(page_map)
        except Exception as e:  # noqa: BLE001
            run_log(run_id, "DISCOVER", f"discovery unavailable ({e}); using fallbacks.")
            return dict(_FALLBACK_SELECTORS), "page not discovered"

    def _spec_path(self, ticket_key: str) -> Path:
        root = Path(__file__).resolve().parents[3]
        return root / "playwright" / "suites" / "p0" / f"{ticket_key}.spec.ts"

    def _spec_relative(self, ticket_key: str) -> str:
        # testDir is './suites', so the CLI path is relative to suites/
        return f"p0/{ticket_key}.spec.ts"

    def _report_dir_relative(self, ticket_key: str) -> str:
        # RESULTS_BASE in playwright.config.ts: results/<TICKET_KEY> when set,
        # so a second ticket's report never overwrites the first one's.
        return f"results/{ticket_key}"

    def _html_report_path(self, ticket_key: str) -> str:
        root = Path(__file__).resolve().parents[3]
        return str(root / "playwright" / self._report_dir_relative(ticket_key)
                  / "html-report" / "index.html")

    def _allure_report_path(self, ticket_key: str) -> str:
        root = Path(__file__).resolve().parents[3]
        return str(root / "playwright" / self._report_dir_relative(ticket_key)
                  / "allure-report" / "index.html")

    def _generate_allure_report(self, run_id: str, ticket_key: str) -> None:
        """Renders results/<ticket>/allure-results/ into the static site at
        results/<ticket>/allure-report/ (npm run report:generate) — reporting is
        part of the automated pipeline, not a manual step after the fact. Never
        raises: a report-generation failure must not block a PR for a run whose
        tests actually passed."""
        pw = Path(__file__).resolve().parents[3] / "playwright"
        npm = "npm.cmd" if sys.platform == "win32" else "npm"
        env = {**os.environ, "TICKET_KEY": ticket_key}
        try:
            res = subprocess.run([npm, "run", "report:generate"], cwd=str(pw),
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=120, env=env)
            if res.returncode == 0:
                run_log(run_id, "REPORT", f"Allure report generated at {self._allure_report_path(ticket_key)}")
            else:
                run_log(run_id, "REPORT", f"Allure report generation failed: {res.stderr[-500:]}")
        except (OSError, subprocess.TimeoutExpired) as e:
            run_log(run_id, "REPORT", f"Allure report generation errored: {e}")

    def _stage_attachments(self, run_id: str, ticket_key: str,
                           scenarios: list[dict]) -> dict[str, str]:
        """Download every Jira attachment referenced by an ``upload_file`` step into
        ``playwright/fixtures/attachments/<TICKET>/`` and return {filename: local_path}.

        Generic across tickets: it only ever fetches filenames the DSL actually asked
        for, so a ticket with no upload step never touches Jira attachments at all.
        """
        wanted: set[str] = set()
        for sc in scenarios:
            for st in sc.get("steps", []):
                if st.get("action") == "upload_file":
                    wanted.update(n.strip() for n in st.get("value", "").split(",") if n.strip())
        if not wanted:
            return {}

        dest_dir = (Path(__file__).resolve().parents[3]
                   / "playwright" / "fixtures" / "attachments" / ticket_key)
        staged: dict[str, str] = {}
        try:
            available = {a.filename: a for a in self.d.jira.fetch_attachments(ticket_key)}
        except Exception as e:  # noqa: BLE001
            run_log(run_id, "ATTACH", f"could not list Jira attachments ({e})")
            return staged

        for filename in wanted:
            existing = dest_dir / filename
            if existing.exists():
                staged[filename] = str(existing)
                continue
            attachment = available.get(filename)
            if not attachment:
                run_log(run_id, "ATTACH", f"'{filename}' not found among ticket attachments")
                continue
            try:
                path = self.d.jira.download_attachment(attachment, dest_dir)
                staged[filename] = str(path)
                run_log(run_id, "ATTACH", f"staged {filename} from Jira attachment")
            except Exception as e:  # noqa: BLE001
                run_log(run_id, "ATTACH", f"failed to download '{filename}' ({e})")
        return staged

    def _run_playwright(self, run_id: str, ticket_key: str, cfg: dict) -> tuple[bool, str]:
        if self.d.run_tests is not None:          # injected runner (tests/CI)
            return self.d.run_tests(self._spec_relative(ticket_key))
        pw = Path(__file__).resolve().parents[3] / "playwright"
        npx = "npx.cmd" if sys.platform == "win32" else "npx"
        # No --reporter override: playwright.config.ts's own reporter array (list,
        # junit, html, allure-playwright) runs, so every ticket run also produces
        # an Allure result — not just Jenkins regression runs. 'list' still comes
        # first, so stdout tails the same way for the Jira comment below.
        cmd = [npx, "playwright", "test", self._spec_relative(ticket_key),
               "--project=P0", "--project=P1", "--project=P2"]
        if self.d.settings.playwright_headed:
            cmd.append("--headed")
        # TICKET_KEY routes junit/html-report/allure-results into
        # results/<ticket_key>/ (playwright.config.ts), matching this repo's
        # existing per-ticket results folder convention. TEST_ENV/BASE_URL come
        # from the SAME cfg that rendered the spec — the report's Environment
        # widget must always reflect the real environment/URL this run actually
        # used (staging, sandbox, prod, whatever), never a guessed/static default.
        # TEST_USER/TEST_PASS must be passed explicitly: pydantic-settings
        # parses .env into the Settings object only — it never copies those
        # values into os.environ, so {**os.environ, ...} alone leaves the
        # generated spec's process.env.TEST_USER/TEST_PASS undefined and every
        # credential field gets filled with an empty string.
        env = {**os.environ, "TICKET_KEY": ticket_key,
               "TEST_ENV": cfg["env_name"], "BASE_URL": cfg["login_url"],
               "TEST_USER": self.d.settings.test_user, "TEST_PASS": self.d.settings.test_pass}
        try:
            # Explicit UTF-8: on Windows, text=True without an encoding decodes
            # subprocess output with the system codepage (cp1252), which mangles
            # Playwright's UTF-8 console glyphs (›, —, ✓) into mojibake wherever
            # that output ends up (e.g. the Jira completion comment).
            res = subprocess.run(cmd, cwd=str(pw), capture_output=True,
                                 text=True, encoding="utf-8", errors="replace",
                                 timeout=900, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"Playwright could not run: {e}"
        return self._summarize_junit(pw, ticket_key, res.returncode == 0)

    def _summarize_junit(self, playwright_dir: Path, ticket_key: str,
                         returncode_ok: bool) -> tuple[bool, str]:
        """Build a clean, human-readable summary from results/<TICKET>/junit.xml
        instead of scraping raw console output — avoids both the encoding mess
        above and the cryptic `file:line › ... | | ...` formatting of the list
        reporter's tail."""
        junit_path = playwright_dir / self._report_dir_relative(ticket_key) / "junit.xml"
        if not junit_path.exists():
            return returncode_ok, "Playwright ran but produced no junit.xml."
        cases = [c for s in junit_parser.parse_files([junit_path]) for c in s.cases]
        if not cases:
            return returncode_ok, "Playwright ran but no test cases were reported."
        failed = [c for c in cases if c.is_failure]
        passed = len(cases) - len(failed)
        lines = [f"{passed}/{len(cases)} passed"]
        lines += [f"- {'FAILED' if c.is_failure else 'passed'} — {c.name} ({c.time:.1f}s)"
                 for c in cases]
        if failed:
            lines.append("")
            lines += [f"Failure — {c.name}: {c.message}" for c in failed]
        return (returncode_ok and not failed), "\n".join(lines)

    def _plan_markdown(self, ticket: str, cfg: dict, selectors: dict,
                       scenarios: list[dict], source: str, note: str = "") -> str:
        sel_parts = [f"email `{selectors.get('email')}`"]
        if selectors.get("password"):
            sel_parts.append(f"password `{selectors['password']}`")
        if selectors.get("submit"):
            sel_parts.append(f"submit `{selectors['submit']}`")
        if selectors.get("iframe"):
            sel_parts.append(f"iframe `{selectors['iframe']}`")
        if not selectors.get("password"):
            sel_parts.append("_(password/submit resolved live by the action-DSL — "
                             "progressive login, not on the page at first load)_")

        lines = [f"# {'Revised plan' if note else 'Plan'} for {ticket}", ""]
        if source == "fallback":
            # A silent degrade here is the one failure mode that actually
            # matters at scale: the more scenarios a ticket's description
            # describes, the more surface area there is for a single LLM call
            # to fail (CLI error, unparsable/truncated output, no runner
            # configured) -- and on failure this run still produces a
            # perfectly normal-looking plan comment with 4 generic login-only
            # scenarios, none of which are about this ticket at all. Burying
            # that in one plain "**Scenario source:** fallback" line among a
            # dozen other plain lines is exactly how a reviewer approves an
            # inadequate plan without ever noticing — this has to be the very
            # first thing in the comment, not a detail to notice while reading.
            lines += ["> ⚠️ **SCENARIO DESIGN FALLBACK — these are generic "
                     "built-in scenarios, NOT designed from this ticket.** "
                     "The LLM design call was unavailable, failed, or "
                     "returned output that couldn't be parsed (check the run "
                     "log). Do not approve this plan for a real ticket — fix "
                     "the underlying issue and re-trigger the run instead.", ""]
        lines += [f"**Environment:** `{cfg['env_name']}` — {cfg['login_url']}",
                 "**Selectors (discovered):** " + ", ".join(sel_parts),
                 f"**Scenario source:** {source}", "", "## Scenarios", ""]
        for s in scenarios:
            lines.append(f"### `{s['tag']}` {s['title']}")
            lines.append("")
            lines.extend(self._scenario_step_lines(s["steps"]))
            lines.append("")
        if note:
            lines += ["## Revision requested", "", note, ""]
        lines += ["## Implementation",
                  f"- Playwright spec: `{self._spec_relative(ticket)}`",
                  "- Rendered from a closed action-DSL, then gated "
                  "(test count, bracket balance, required selectors).",
                  "", "Approve this plan to generate the spec, run it, and open the PR."]
        return "\n".join(lines)

    def _scenario_step_lines(self, steps: list[dict]) -> list[str]:
        """Group consecutive steps sharing a DSL "section" under a bold, numbered
        sub-header — the same grouping generator.py already uses to wrap same-section
        steps into one collapsible test.step() block. Without this, a reviewer sees
        one 60-line flat bullet list instead of the same Login / Workspace / Folder /
        Upload / Download phases the eventual test report groups them into, which is
        what actually makes a long workflow scannable at a glance.

        A step with no "section" (see designer.py) renders inline, unwrapped — same
        rule generator.py follows for the generated spec.
        """
        lines: list[str] = []
        current_section = None
        section_num = 0
        for st in steps:
            section = st.get("section")
            if section != current_section and section:
                section_num += 1
                if lines:
                    lines.append("")
                lines.append(f"**{section_num}. {section}**")
                lines.append("")
            current_section = section
            lines.append(f"- {self._describe_step(st)}")
        return lines

    @staticmethod
    def _describe_step(st: dict) -> str:
        """One human-readable line per DSL step, generic across every action —
        this is what lets a reviewer see exactly what will run without reading
        the generated TypeScript."""
        a = st.get("action")
        target, value = st.get("target"), st.get("value")
        return {
            "fill": f"Enter {st.get('field')}: `{value}`",
            "click": f"Click {st.get('field')}",
            "wait_url": "Wait for the post-login redirect",
            "expect_url": "Verify redirected to the home URL",
            "expect_not_url": "Verify still on the login page",
            "wait_ms": f"Wait {value}ms",
            "click_text": f"Click '{target}'",
            "dblclick_text": f"Double-click '{target}'",
            "fill_named": f"Enter {target}: `{value}`",
            "expect_visible": f"Verify '{target}' is visible",
            "type_search": f"Search '{target}' for `{value}`",
            "add_list_item": f"Add `{value}` to {target}",
            "upload_file": f"Upload file(s): {value}",
            "select_all": f"Check '{target or 'Select all'}'",
            "annotate": f"Record {target}: `{value}`",
            "view_file_preview": f"Open '{target}' preview (new tab), verify the "
                                  "document/PDF viewer is visible, then close it",
            "select_first_swatch": f"Pick the first available {target}",
            "reload_page": "Reload the page (force a fresh fetch)",
            "download": f"Click '{target or 'Download'}' and verify the download",
            "expect_zip_contains": f"If a ZIP downloaded, verify it contains: {value}",
        }.get(a, a)

    def _safe_comment(self, ticket_key: str, text: str) -> None:
        try:
            if ticket_key:
                self.d.jira.post_comment(ticket_key, text)
        except Exception:  # noqa: BLE001
            pass

    def _safe_transition(self, ticket_key: str, status: str) -> None:
        try:
            if ticket_key:
                self.d.jira.transition(ticket_key, status)
        except Exception:  # noqa: BLE001
            pass

    def _safe_jira_error(self, ticket_key: str, error: Exception) -> None:
        self._safe_comment(ticket_key,
                           f"UI automation FAILED: {type(error).__name__}: {error}")
