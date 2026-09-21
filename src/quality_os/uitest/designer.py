"""designer.py — Sonnet 4.6 designs scenarios as a tiny action DSL (JSON).

TRANSPORT: this calls the host's already-authenticated Claude Code CLI
(``claude -p --agent coding/ui-test-designer``, see clients/claude_runner.py) —
i.e. the Claude Pro/Max subscription login — never the metered Anthropic HTTP
API or an ``ANTHROPIC_API_KEY``. The persona/instructions live in
``.claude/agents/coding/ui-test-designer.md``; this module only builds the
user prompt, validates the returned DSL, and caches it.

TOKEN STRATEGY (why this is cheap, without ever running on stale input):
  * ONE call per (ticket key, reviewer note, persona-file fingerprint, current
    requirement text + page summary) combination — see _cache_path. An
    unedited, already-approved ticket re-run costs 0 tokens (identical text →
    cache hit); editing the ticket's description, adding a new human comment,
    posting a Gate-1 "request changes" note, or editing this persona file each
    independently bust the cache and produce a fresh design on the very next
    run, with no manual .cache/uitest cleanup ever required.
  * Input bounded but generous: ticket text ≤200,000 chars (see
    _TICKET_TEXT_MAX — a last-resort guard against a truly pathological
    paste, not a real-world limit; a heavily-commented, actively-iterated
    ticket easily reaches tens of thousands of characters on its own and
    must never be truncated for that, so this is sized around Claude's own
    large context window instead — a real truncation is always logged,
    never silent) + compact page map ≤1500 chars.
  * Output is strict JSON DSL, no verbose prose and no code — code is rendered
    deterministically later.
ACCURACY STRATEGY:
  * The LLM never writes TypeScript. It emits actions from a closed set — the
    renderer maps them to code — invalid actions are dropped, so broken syntax
    is impossible.
  * Two families of action:
      - LOGIN_ACTIONS (fill/click/wait_url/...) target the pre-*discovered*
        login field selectors (field: email|password|submit) — zero ambiguity.
      - GENERIC_ACTIONS (click_text/fill_named/upload_file/download/...) target
        anything else in the app by its visible text/label ("target"), so the
        same DSL can drive an arbitrary multi-page journey (workspace/folder/
        upload/download flows, not just a login form) without new discovery
        code for every new screen. The renderer turns "target" into a resilient
        Playwright role/text locator, never raw code.
  * ``${NOW}`` in any value is resolved by the renderer (not the LLM) to the
    current timestamp — used for ticket-style dynamic names like
    "Automation + Current DateTime".
  * If no Claude Code runner is wired up, or the call fails, or it returns
    unparsable output → known-good default login scenarios are used, so a
    live run can NEVER break.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

# repo root = .../src/quality_os/uitest/designer.py -> parents[3]
ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = ROOT / ".cache" / "uitest"
AGENT_MD_PATH = ROOT / ".claude" / "agents" / "coding" / "ui-test-designer.md"

# ---- closed action vocabulary --------------------------------------------
LOGIN_ACTIONS = {"fill", "click", "wait_url", "expect_url", "expect_not_url", "wait_ms"}
GENERIC_ACTIONS = {
    "click_text",          # click any button/link by its visible text
    "dblclick_text",       # double-click a row/item by its visible text — some
                           # apps (e.g. a file list) only open a preview on a
                           # double-click, never a single one; use this instead
                           # of click_text when the ticket calls for opening/
                           # previewing an item rather than selecting it
    "click_text_optional", # click by visible text ONLY if it's currently on
                           # screen, otherwise do nothing — use this instead of
                           # click_text for a control whose presence depends on
                           # state left by an earlier step (e.g. "Clear
                           # Selection" only shows up if something is still
                           # selected); click_text itself has no such
                           # tolerance and will hang until the test timeout
                           # waiting for text that may never appear
    "fill_named",          # fill any field identified by its label/placeholder
    "expect_visible",      # assert some text/element is on screen
    "type_search",         # type into a search box, then press Enter
    "add_list_item",       # repeatable multi-value field (e.g. invite N members)
    "upload_file",         # attach one or more files (comma-separated filenames)
    "select_all",          # click a "select all" checkbox
    "download",            # click a control that starts a download; verifies it landed
    "expect_zip_contains", # assert the most recent download (if a ZIP) contains files
    "select_first_swatch", # pick the first icon/color/avatar option from an unlabeled
                           # visual picker (no ARIA name to hook a text-based action into)
    "reload_page",        # force a fresh fetch of the current page — a just-created
                           # item can be confirmed server-side yet missing from an
                           # already-loaded, stale client-side list until reloaded
    "open_row_menu",       # open a specific row's "..." options menu (file/folder/
                           # workspace listings) — scoped by that row's visible text,
                           # since every row's menu trigger shares the same name (or
                           # no name at all)
    "check_named",        # check one specific checkbox by its own accessible name
                           # (e.g. a single file in a multi-row list) — select_all is
                           # for the header "select all" control, this is for one row
    "select_dropdown_option",  # switch a view-selector combobox (e.g. "My
                               # Workspaces" <-> "Archived Workspaces") by clicking
                               # the option with this visible text
    "capture_files",       # record the file/folder names ACTUALLY present in the
                           # current listing (by each row checkbox's own accessible
                           # name) under a label, for a later expect_same_files to
                           # compare against -- use this instead of hardcoding a
                           # ticket's named test files when verifying a move/copy
                           # of files that already exist in the environment (not
                           # ones this scenario just uploaded): the ticket's stated
                           # filenames can drift out of sync with what's actually
                           # in the environment, so the source of truth is
                           # whatever is really there, not the ticket text
    "expect_same_files",   # assert the current listing has the exact same files as
                           # an earlier capture_files under the same label (reports
                           # which are missing/unexpectedly extra on failure)
    "annotate",            # record a label/value fact (e.g. the dynamic workspace
                           # or folder name, which files were uploaded) so a
                           # reviewer can see it at the top of the HTML report and
                           # cross-check the live UI without hunting through steps
    "capture_screenshot",  # attach a full-page screenshot to the test report under
                           # a short label, regardless of whether the test later
                           # passes or fails — the built-in screenshot setting only
                           # ever fires once, on failure, so it gives a reviewer no
                           # visual evidence at all for a passing run; use this
                           # right after a state transition that has no other
                           # naturally screenshot-worthy on-screen assertion (e.g.
                           # immediately after a "move files between folders" step
                           # completes, once per direction) so a human reading the
                           # report can see, not just trust, that it actually happened
    "view_file_preview",   # click a file's name (a real hyperlink) to open its
                           # preview, which opens in a NEW browser tab (confirmed
                           # live — the document viewer is not inline) — confirm it
                           # rendered a page, then close that tab. One atomic
                           # action because the popup page it opens only exists
                           # as a local variable inside the step's own generated
                           # block; it can't be threaded through as separate
                           # click/verify/close DSL steps. Fixed viewer
                           # selectors, same reasoning as select_first_swatch's
                           # closed CSS.
}
ALLOWED_ACTIONS = LOGIN_ACTIONS | GENERIC_ACTIONS
ALLOWED_FIELDS = {"email", "password", "submit"}          # legacy 'click'/'fill' actions only
ALLOWED_VALUES = {"${USER}", "${PASS}", "${WRONG_PASS}", ""}  # legacy exact-match placeholders
# select_first_swatch's target is a closed keyword, never a raw CSS selector — the
# renderer owns the fixed pattern each keyword maps to (see generator.py).
SWATCH_KINDS = {"icon", "color", "avatar"}

_TARGET_MAX = 80
_VALUE_MAX = 200
# A ceiling on the ticket text this module will ever hash or send to the LLM
# -- NOT a scenario-count limiter, and deliberately generous: requirement_text()
# is description + every human comment (CLAUDE.md rule 2: "newest comments
# carry the latest intent"), and a ticket that's been iterated on for a while
# legitimately accumulates a large comment thread on its own, with no
# pathologically-huge single paste involved at all (confirmed live: a real,
# actively-used ticket's requirement_text() was 49,863 chars from ordinary
# back-and-forth comments alone -- an earlier, much lower ceiling here
# silently cut real requirement content, and truncation is exactly the
# failure mode this system must never cause quietly). Runs are triggered one
# ticket at a time, never concurrently, so there is no cost/throughput reason
# to cap this tightly -- it exists purely as a last-resort guard against a
# truly pathological paste (an entire unrelated document, a multi-MB log
# dump), sized with Claude's own large context window in mind, and it never
# truncates silently — see the log line in design_scenarios() where it's
# applied.
_TICKET_TEXT_MAX = 200_000
# upload_file / expect_zip_contains values are filenames only (never arbitrary text) —
# guards against path traversal once the generator turns them into real file paths.
_FILENAMES_RE = re.compile(r"^[A-Za-z0-9_.,\- ]{1,200}$")

# The designer persona/instructions (schema, the closed action vocabulary, scenario
# rules) live in .claude/agents/coding/ui-test-designer.md — that's what Claude Code
# loads for `claude -p --agent coding/ui-test-designer`. Keep that file in sync with
# ALLOWED_ACTIONS/GENERIC_ACTIONS below whenever the vocabulary changes.

# Known-good fallback (the proven login set) — used if LLM unavailable/fails.
# Intentionally simple: it is the safety net for when there is no AI available at
# all, not a substitute for a real multi-step design.
FALLBACK = {"scenarios": [
    {"id": "SC-1", "title": "{T} P0 — valid credentials redirect to home @p0",
     "tag": "@p0", "comment": "POSITIVE — valid credentials redirect to home",
     "steps": [{"action": "fill", "field": "email", "value": "${USER}"},
               {"action": "fill", "field": "password", "value": "${PASS}"},
               {"action": "click", "field": "submit"},
               {"action": "wait_url"}, {"action": "expect_url"}]},
    {"id": "SC-2", "title": "{T} P1 — wrong password stays on login @p1",
     "tag": "@p1", "comment": "NEGATIVE — wrong password stays on login",
     "steps": [{"action": "fill", "field": "email", "value": "${USER}"},
               {"action": "fill", "field": "password", "value": "${WRONG_PASS}"},
               {"action": "click", "field": "submit"},
               {"action": "wait_ms", "value": "4000"},
               {"action": "expect_not_url"}]},
    {"id": "SC-3", "title": "{T} P1 — empty email blocks login @p1",
     "tag": "@p1", "comment": "NEGATIVE — empty email blocks login",
     "steps": [{"action": "fill", "field": "password", "value": "${PASS}"},
               {"action": "click", "field": "submit"},
               {"action": "wait_ms", "value": "3000"},
               {"action": "expect_not_url"}]},
    {"id": "SC-4", "title": "{T} P2 — empty password blocks login @p2",
     "tag": "@p2", "comment": "NEGATIVE — empty password blocks login",
     "steps": [{"action": "fill", "field": "email", "value": "${USER}"},
               {"action": "click", "field": "submit"},
               {"action": "wait_ms", "value": "3000"},
               {"action": "expect_not_url"}]},
]}


def _agent_instructions_fingerprint() -> str:
    """A short hash of the designer persona file's own content — folded into
    the cache key so an intentional edit to `ui-test-designer.md` (new
    guidance after a live bug fix, a new DSL action, a corrected pattern for
    this app) automatically busts every ticket's cached scenario, with no one
    needing to remember to clear .cache/uitest by hand (observed in practice: a
    fresh run replayed a stale cached step even after the persona file was
    corrected, because nothing tied the cache to the instructions that
    produced it, until this fingerprint was added).
    This is a different kind of "content" than the ticket text/page summary
    _cache_path deliberately ignores below: the persona file only changes on
    a deliberate commit, never on incidental Jira comment growth, so it has
    none of the permanent-drift problem that ruled those out.
    """
    try:
        return hashlib.sha1(AGENT_MD_PATH.read_bytes()).hexdigest()[:10]
    except FileNotFoundError:
        return "no-agent-md"


def _cache_path(ticket_key: str, extra_note: str,
                ticket_text: str = "", page_summary: str = "") -> Path:
    """Keyed on (ticket, reviewer note, designer persona fingerprint, AND the
    ticket's own current requirement text/page summary).

    Hashing in the ticket's live text was originally ruled out here because it
    used to cause permanent cache drift: requirement_text() included this
    system's own posted comments (plan/approval/completion/failure notices),
    so every run added new content and the hash never stabilised, even for an
    already-approved ticket nobody had touched. That drift source has since
    been fixed one layer down, in `JiraTicket.requirement_text()` (see
    `_BOT_COMMENT_PREFIXES` in `clients/jira_client.py`), which strips this
    system's own comments before the text ever reaches here — so the text
    reaching this function today is only ever human-authored (the description
    plus any human comment). That makes it safe, and correct, to hash: an
    edited description or a newly added "please also cover X" comment now
    busts the cache and produces a fresh design on its own (confirmed live: a
    ticket's description was extended with new negative scenarios and a fresh
    run kept silently replaying the old happy-path-only design, because
    nothing tied the cache to the requirement text that produced it) — while a
    plain re-run of an unedited, already-approved ticket still cache-hits,
    since requirement_text() for it is byte-identical to last time.

    A new reviewer note (Gate 1 "request changes") and an edit to the
    designer's own instructions (`ui-test-designer.md`, see
    `_agent_instructions_fingerprint`) remain their own explicit,
    always-effective cache-busters on top of this.
    """
    h = hashlib.sha1(
        f"{extra_note}|{_agent_instructions_fingerprint()}|{ticket_text}|{page_summary}".encode()
    ).hexdigest()[:10]
    return CACHE_DIR / f"scenarios_{ticket_key}_{h}.json"


_LLM_RETRY_ATTEMPTS = 3   # residual safety net only -- see _call_claude
_LLM_RETRY_DELAY_SECONDS = 2.0
_LLM_DIAG_SNIPPET_MAX = 200  # forensic log snippet only, never used for parsing
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_json(raw: str) -> dict | None:
    """Pull the DSL JSON object out of a claude -p response that is SUPPOSED
    to be JSON-only but, empirically, isn't always: observed in practice,
    the model prefaced its answer with a plain-English confirmation
    sentence before the fence -- 'Confirmed: progressive login (...). Now
    emitting the JSON.\\n\\n```json\\n{...}' -- despite the persona instructing
    "JSON only, no prose, no markdown fences". An LLM instruction is guidance,
    never a hard guarantee, so this must tolerate the model not following it
    to the letter rather than assume byte 0 of the response is '{'. The old
    parser only stripped a fence marker if it was the very first thing in the
    string -- any preamble text made it try to json.loads() the preamble
    itself and fail deterministically at "line 1 column 1", every time,
    regardless of retries (retries only ever helped because a RETRIED call
    sometimes happened not to include a preamble, not because the parsing
    improved).

    Tries, in order: a ```json fenced block anywhere in the text (handles a
    preamble before the fence, the original always-supported case, and
    anything else wrapped in fences); then the first balanced JSON value
    found anywhere in the raw text via `raw_decode`, which parses one value
    starting at a given index and ignores anything after its closing brace --
    this also covers a preamble with NO fence at all. Returns None only if
    neither strategy finds a parseable object anywhere in the text.
    """
    text = raw.strip()
    m = _JSON_FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass  # fall through to the brace-scan below
    idx = text.find("{")
    while idx != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text, idx)
            return obj
        except json.JSONDecodeError:
            idx = text.find("{", idx + 1)
    return None


def _call_claude(claude, user_prompt: str, log,
                 max_attempts: int = _LLM_RETRY_ATTEMPTS) -> dict | None:
    """Ask the ``coding/ui-test-designer`` sub-agent for scenarios via the host's
    logged-in Claude Code CLI (``claude`` is a ClaudeRunner — see
    clients/claude_runner.py). Runs from the repo ROOT so the CLI resolves
    ``.claude/agents/coding/ui-test-designer.md`` regardless of the server's cwd.

    Extraction is now robust to a prose preamble before the JSON (see
    _extract_json) — that was the actual, deterministic cause of every
    "unparsable output" observed in practice. Retrying up to ``max_attempts``
    times remains as a residual
    safety net for whatever this still doesn't cover (e.g. the model
    genuinely produces no JSON anywhere in its answer) — cheap, since a
    successful call already answers in well under a minute. A hard failure
    (``res.ok`` is False — CLI not found, a real error, a 30-minute timeout)
    is deliberately NOT retried: it's either not something an immediate retry
    fixes, or (for a timeout) retrying would mean waiting up to another full
    timeout on top of the first, which is far worse than just falling back
    once.

    Every failed attempt logs the raw output length and a short snippet
    (never used for parsing, diagnostics only) — if this ever needs
    escalating to Anthropic, that's the evidence a bare "unparsable" message
    can't provide on its own.
    """
    for attempt in range(1, max_attempts + 1):
        res = claude.run_agent("coding/ui-test-designer", user_prompt, cwd=str(ROOT))
        if not res.ok:
            log(f"[LLM] claude -p failed → fallback scenarios. ({res.error or 'no output'})")
            return None
        will_retry = attempt < max_attempts
        if not res.output.strip():
            log(f"[LLM] empty response on attempt {attempt}/{max_attempts} "
                f"(raw output was {len(res.output)} chars before stripping)"
                + (" — retrying." if will_retry else " — giving up."))
        else:
            payload = _extract_json(res.output)
            if payload is not None:
                return payload
            snippet = res.output.strip()[:_LLM_DIAG_SNIPPET_MAX]
            log(f"[LLM] no parseable JSON found on attempt {attempt}/{max_attempts}; "
                f"{len(res.output)} chars, starts with: {snippet!r}"
                + (" — retrying." if will_retry else " — giving up."))
        if will_retry:
            time.sleep(_LLM_RETRY_DELAY_SECONDS)
    log("[LLM] all attempts returned unusable output → fallback scenarios.")
    return None


_SECTION_MAX = 40


def _sanitize_step(st: dict) -> dict | None:
    """Validate one DSL step. Returns None to drop it."""
    clean = _sanitize_step_core(st)
    if clean is None:
        return None
    # Optional passthrough, not an action of its own: groups consecutive steps
    # under one collapsible test.step() in the HTML report (e.g. "Create
    # Workspace", "Archive Files") instead of one long flat list — purely a
    # report-readability aid, the renderer runs identically either way.
    section = str(st.get("section", ""))[:_SECTION_MAX].strip()
    if section:
        clean["section"] = section
    return clean


def _sanitize_step_core(st: dict) -> dict | None:
    a = st.get("action")
    if a not in ALLOWED_ACTIONS:
        return None

    if a in ("fill", "click"):
        if st.get("field") not in ALLOWED_FIELDS:
            return None
        return {"action": a, "field": st.get("field", ""),
                "value": str(st.get("value", ""))[:_VALUE_MAX]}

    if a in ("wait_url", "expect_url", "expect_not_url"):
        return {"action": a}

    if a == "wait_ms":
        return {"action": a, "value": str(st.get("value", ""))[:20]}

    if a == "reload_page":
        return {"action": a}

    # --- generic actions: free-text target/value, capped and later escaped by the
    # renderer before being embedded in generated TypeScript, so arbitrary ticket
    # text is safe (treated as a string literal, never as code). ---
    target = str(st.get("target", ""))[:_TARGET_MAX].strip()
    value = str(st.get("value", ""))[:_VALUE_MAX].strip()

    if a in ("click_text", "dblclick_text", "click_text_optional", "view_file_preview",
            "expect_visible", "select_all", "open_row_menu", "check_named",
            "select_dropdown_option", "capture_files", "expect_same_files",
            "capture_screenshot"):
        if a == "select_all" and not target:
            target = "Select all"
        if not target:
            return None
        step = {"action": a, "target": target}
        return step

    if a == "select_first_swatch":
        if target.lower() not in SWATCH_KINDS:
            return None
        return {"action": a, "target": target.lower()}

    if a in ("fill_named", "type_search", "add_list_item", "annotate"):
        if not target or not value:
            return None
        return {"action": a, "target": target, "value": value}

    if a == "download":
        if not target:
            target = "Download"
        return {"action": a, "target": target}

    if a in ("upload_file", "expect_zip_contains"):
        if not value or not _FILENAMES_RE.match(value):
            return None
        return {"action": a, "value": value}

    return None


def _sanitize(payload: dict, ticket_key: str) -> list[dict]:
    """Validate the DSL: drop anything outside the closed action set."""
    out = []
    for i, sc in enumerate(payload.get("scenarios", []), 1):
        steps = []
        for st in sc.get("steps", []):
            clean = _sanitize_step(st)
            if clean:
                steps.append(clean)
        if not steps:
            continue
        title = str(sc.get("title", f"{ticket_key} scenario {i}"))[:90]
        tag = sc.get("tag") if sc.get("tag") in ("@p0", "@p1", "@p2") else "@p1"
        if tag not in title:
            title = f"{title} {tag}"
        out.append({"id": sc.get("id", f"SC-{i}"), "title": title, "tag": tag,
                    "comment": str(sc.get("comment", ""))[:120], "steps": steps})
    return out


def design_scenarios(cfg: dict, ticket_text: str, page_summary: str,
                     log=print, extra_note: str = "", claude=None) -> tuple[list[dict], str]:
    """Return (scenarios, source) where source is 'llm' | 'cache' | 'fallback'.

    ``claude`` is a ClaudeRunner (clients/claude_runner.py) — it shells out to the
    host's already-authenticated Claude Code CLI (Pro/Max subscription login),
    never a metered Anthropic API key. Pass ``None`` (e.g. in tests, or if the
    workflow has no runner wired up) to always use the fallback scenarios.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = cfg["ticket"]
    if len(ticket_text) > _TICKET_TEXT_MAX:
        # Loud, not silent: a truncation here means some real scenario text
        # got cut off, which is exactly the failure this system must never
        # hide (see the "fallback" warning banner in uitest_workflow.py's
        # _plan_markdown for the sibling case — an LLM call that fails
        # outright). This should not fire for a normal many-scenario ticket;
        # if it does, it's a signal the ticket itself needs trimming.
        log(f"[DESIGN] ticket text is {len(ticket_text)} chars — truncating to "
            f"{_TICKET_TEXT_MAX} (some scenario text may be cut off; consider "
            "trimming the ticket description).")
        ticket_text = ticket_text[:_TICKET_TEXT_MAX]
    cp = _cache_path(key, extra_note, ticket_text, page_summary)
    if cp.exists():
        log(f"[DESIGN] cache hit → {cp.name} (0 tokens)")
        return json.loads(cp.read_text(encoding="utf-8")), "cache"

    fallback = json.loads(json.dumps(FALLBACK).replace("{T}", key))["scenarios"]
    if claude is None:
        log("[DESIGN] no Claude Code runner configured — using built-in fallback scenarios.")
        return fallback, "fallback"

    prompt = (f"{ticket_text}\n\nDISCOVERED PAGE FIELDS:\n{page_summary}\n")
    if extra_note:
        prompt += f"\nADDITIONAL REVIEWER REQUEST (add scenario(s) for this):\n{extra_note}\n"
    prompt += "\nReturn the JSON now."

    payload = _call_claude(claude, prompt, log)
    scenarios = _sanitize(payload, key) if payload else []
    if not scenarios:
        log("[DESIGN] LLM output unusable — using fallback scenarios.")
        return fallback, "fallback"

    cp.write_text(json.dumps(scenarios, indent=2), encoding="utf-8")
    log(f"[DESIGN] {len(scenarios)} scenarios designed by "
        f"claude -p (coding/ui-test-designer) → cached {cp.name}")
    return scenarios, "llm"
