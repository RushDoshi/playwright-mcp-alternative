# playwright-mcp-alternative

**An AI-native QA automation host that generates and runs Playwright UI tests from a Jira ticket — without the Playwright MCP server, and without a metered Anthropic API key.**

It reads a ticket, discovers the target page deterministically, asks Claude for a *closed action-DSL* (never raw code), renders the `.spec.ts` itself, gates it, runs it, and reports back to Jira — with a human approval gate in the middle.

---

## Why this exists — a safer alternative to the Playwright MCP

Giving an LLM agent a live browser via the Playwright MCP server is powerful, but it hands the model an open-ended tool surface: arbitrary navigation, arbitrary actions, arbitrary code, driven directly by model output. For a QA pipeline running against real environments with real credentials, that is a meaningful security and blast-radius concern.

**This project takes the opposite split of responsibility:**

- The **model chooses *what* to test** — it emits scenarios as JSON from a **closed vocabulary of actions** (`click_text`, `fill_named`, `upload_file`, `download`, …). It never writes TypeScript.
- The **host decides *how* it is written** — a deterministic renderer maps each validated action onto a fixed code template, with all free text escaped into string literals. Anything outside the closed action set is dropped.

Because of that split:

- **The LLM can never emit executable code or an un-vetted action** — broken syntax and surprise tool calls are impossible by construction. (See the sanitizer test in `tests/` that drops an out-of-vocabulary action.)
- **Credentials are never sent to the model and never baked into generated tests** — they are read from `process.env` at run time. A generation gate fails the build if a literal ever leaks in.
- **Page discovery costs zero LLM tokens** — Playwright dumps the page structure to a cached `page_map.json`; the model only ever sees a compact text summary.
- **A human approves the plan in the Jira ticket** before anything runs.

## No Anthropic API key required

This runs on your **Claude Pro/Max subscription login** (`claude login`), invoked through the Claude Code CLI as a subprocess — **not** a metered `ANTHROPIC_API_KEY`. Leave the key blank in `.env` and the CLI uses your logged-in session. (See `src/quality_os/clients/claude_runner.py`.)

---

## Prerequisites & known gotchas (read this first)

**0. Use ONE extracted folder.** If you've downloaded/unzipped this more than
once, your OS may have created duplicate folders like `<repo-name>(2)`, `<repo-name>(3)`, etc.
`npm install` and `pip install` are per-folder — installing in one doesn't
install in another. Delete the duplicates and work from a single copy to
avoid mysterious "works here, not there" failures.


These are the real friction points — knowing them upfront saves you the debugging they'd otherwise cost.

1. **Two runtimes, two installs.** The host is Python; running the generated tests is Node/Playwright. You need **both**: `pip install` for the host, and separately `npm install` + `npx playwright install` inside `playwright/`. Skipping the Playwright step means specs generate but can't execute.

2. **Python ≥ 3.10 and Node ≥ 18 required.** The code uses 3.10+ type-union syntax (`str | None`); it will not import on 3.9 or older. Playwright needs a current Node LTS.

3. **The Playwright run path is config-verified but not executed in this release.** The Python host, the spec generator, and all safety gates are fully tested. The `playwright/` project (`package.json`, `playwright.config.ts`, `scripts/generate-report.js`) is syntax-valid and hardened for a clean first run (minimal dependencies; Allure fully optional; no shell-specific or `require`-based config that could crash on load). What has **not** been executed here is an end-to-end `npx playwright test` against a live browser + app (it needs npm network access + a target app). Expect it to work, but validate on a networked machine before relying on it for anything critical — the fastest way is the built-in dummy-app demo (`cd playwright && npm install && npx playwright install && npm run demo`), which runs a generated spec against a local login page with no real app or credentials.

4. **`claude login` is needed for real scenario design.** This uses your Claude Pro/Max subscription via the Claude Code CLI — no API key. **Without** a logged-in CLI, the designer silently falls back to a built-in login-only scenario set (it won't crash, but it won't design from your ticket either). Install the CLI per https://docs.claude.com, then run `claude login`.

5. **Six modules are reference stubs, not real integrations.** `coding_workflow`, `regression_workflow`, and the `bitbucket`/`azure`/`jenkins`/`email` clients let the host boot and their state machines complete, but they perform **no real work** (they log `[STUB] …`). The UI-test workflow is the fully-implemented one. Wire the stubs up before expecting real PRs, load tests, or emails.

6. **Don't move files around.** The package path is hardwired: modules resolve the repo root via `parents[3]` and expect the `src/quality_os/...` layout with `playwright/`, `config/`, and `.cache/` at the repo root. Relocating files breaks path resolution.

7. **Launch uvicorn with `--app-dir src`.** The package lives under `src/`, so `uvicorn quality_os.main:app --app-dir src` is required — plain `uvicorn quality_os.main:app` won't find it.

8. **You must supply a target app + credentials to run real tests.** There is no zero-config "runs a live test on first clone" mode — you provide a `login_url` (in `config/environments.yaml`) and `TEST_USER`/`TEST_PASS` (in `.env`). This is inherent to a UI-test tool. The demo below needs none of this.

9. **Allure reporting is fully opt-in.** The default `npm install` pulls only `@playwright/test` and `adm-zip` (minimal, well-established) — no Allure. If you want Allure output, `npm install allure-playwright` and install the Allure CLI; the config auto-detects it and `npm run report:generate` degrades gracefully (exits cleanly) when it's absent. Runs always produce JUnit + Playwright's own HTML report regardless.

10. **App-specific selectors need adapting.** A couple of DSL actions (`open_row_menu`, `view_file_preview`) ship generic default selectors marked `ADAPT TO YOUR APP` in `generator.py` — see the section near the end of this README.

---

## Try it in 30 seconds (no app, no Jira, no credentials, no LLM call)

This proves the core — deterministic DSL → valid, gated Playwright spec:

```bash
pip install -r requirements.txt
python demo/demo_generate_spec.py
```

You'll see a real `.spec.ts` rendered from a closed action-DSL and passing every safety gate (test count, brace/paren balance, required selectors, credential-leak check). Run the test suite too:

```bash
pip install -r requirements-dev.txt
pytest
```

---

## What works out of the box vs. what you wire up

| Capability | Status on a fresh clone |
|---|---|
| Deterministic spec generation from the action-DSL | ✅ Works now (`demo/`, `pytest`) |
| FastAPI host boots; `/health`, `/runs/*`, all endpoints load | ✅ Works now |
| State machine + human approval gates (Gate 1 & 2) | ✅ Works now |
| Page discovery + running a generated spec | ✅ Needs the `playwright/` project set up (below) + a target app |
| Scenario **design** by Claude | ⚙️ Needs `claude login` (Pro/Max). Without it, a built-in fallback login scenario set is used. |
| Jira read/plan/approval, Bitbucket PRs | ⚙️ Needs your Jira/Bitbucket credentials in `.env` |
| Coding-agent & load-regression workflows | 🧩 Shipped as **reference stubs** — the host runs and the state machines complete, but they perform no real work until you implement them (clearly marked in `orchestrator/coding_workflow.py`, `orchestrator/regression_workflow.py`, and the `clients/*_client.py` stubs). The UI-test workflow is the fully-implemented one. |

There is no zero-config "runs a real test on first clone" mode — a UI-test tool needs *you* to point it at an app with a login. That's expected for this category of tool.

## Full setup

```bash
# 1. Python host
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Claude Code CLI, logged in with your Pro/Max account (no API key)
#    Install per https://docs.claude.com , then:
claude login

# 3. Playwright project (to actually run generated specs)
cd playwright
npm install
npx playwright install       # browser binaries

# Optional: prove the Playwright run path with a built-in dummy app —
# no real app, no credentials needed. Serves a local login page and runs
# a generated spec against it (should pass):
npm run demo
cd ..

# 4. Configure
cp .env.example .env          # fill in Jira + TEST_USER/TEST_PASS
#   edit config/environments.yaml → your login_url

# 5. Run the host
uvicorn quality_os.main:app --app-dir src --reload
```

Trigger a UI-test run for a ticket:

```bash
curl -X POST http://localhost:8000/run/uitest \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Token: $WEBHOOK_SECRET" \
  -d '{"ticket": "PROJ-123"}'
```

Approve the plan by replying **`Approved`** on the Jira ticket (the poller auto-resumes), or:

```bash
curl -X POST http://localhost:8000/uitest/<run_id>/decision \
  -H "X-Webhook-Token: $WEBHOOK_SECRET" \
  -d '{"decision": "approve"}'
```

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/webhook/jira` | Jira "Ready for Development" → start pipeline |
| POST | `/run/uitest` | Manually start a UI-test run for a ticket |
| POST | `/uitest/{run}/decision` | Gate 1 resume (`approve` / `request_changes`) |
| POST | `/run/regression` | Start the (stub) regression workflow |
| POST | `/regression/{run}/decision` | Gate 2 resume (`approve` / `skip`) |
| GET  | `/runs/{run}` | Run status |
| GET  | `/runs/{run}/logs` | Incremental run log |
| GET  | `/health` | Health check |

---

## Architecture

```
Jira ticket ("Ready for Development")
        │
        ▼
  ┌──────────────┐   deterministic, 0 tokens
  │  discovery   │──► page_map.json (cached per URL)
  └──────────────┘
        │  compact page summary
        ▼
  ┌──────────────┐   claude -p (Pro/Max login)
  │  designer    │──► closed action-DSL (JSON), validated + cached
  └──────────────┘
        │
        ▼
  ┌──────────────┐   Gate 1: human approves the plan IN Jira
  │  approval    │
  └──────────────┘
        │
        ▼
  ┌──────────────┐   deterministic template renderer
  │  generator   │──► <TICKET>.spec.ts  ──► generation gates
  └──────────────┘
        │
        ▼
   Playwright run ──► JUnit + HTML + Allure reports ──► back to Jira
```

State is a validated state machine persisted per run, so a run can pause at its gate and resume later:

```
PENDING → CLONING → PLANNING → AWAITING_APPROVAL (Gate 1)
        → IMPLEMENTING → CREATING_PR → DONE          (+ FAILED rail)
```

## Layout

```
src/quality_os/
├── config.py                     # all host settings (env-driven)
├── state.py                      # state machines + persistence
├── main.py                       # FastAPI entry point
├── clients/
│   ├── claude_runner.py          # bridge to the Claude Code CLI (Pro/Max login)
│   ├── jira_client.py            # read ticket, post plan, detect approval
│   ├── bitbucket_client.py       # PR creation           (reference stub)
│   ├── azure_spot_client.py      # Spot VM lifecycle      (reference stub)
│   ├── jenkins_client.py         # regression job trigger (reference stub)
│   └── email_client.py           # report email          (reference stub)
├── gates/
│   ├── approval.py               # the two human gates
│   └── jira_poller.py            # auto-resume Gate 1 from a Jira "Approved" reply
├── orchestrator/
│   ├── uitest_workflow.py        # the UI-test (Playwright) workflow — FULL impl
│   ├── coding_workflow.py        # coding agent          (reference stub)
│   └── regression_workflow.py    # load regression       (reference stub)
├── uitest/
│   ├── discovery.py              # AI-free page discovery (cached)
│   ├── designer.py               # LLM designs scenarios as a closed action-DSL
│   ├── generator.py              # deterministic DSL → TypeScript + safety gates
│   └── envs.py                   # merge environments.yaml with host secrets
├── domain/
│   └── junit_parser.py           # JUnit/Surefire XML → pass/fail summary
└── utils/
    ├── errors.py                 # FAILED-rail handling
    └── logging.py                # structured run logging
config/environments.yaml          # per-environment URLs/timeouts (non-secret)
demo/demo_generate_spec.py        # zero-setup generation demo
tests/                            # pytest smoke tests (no external deps)
playwright/                       # Playwright project that runs generated specs
```

## Adapting the selectors to your app

A few DSL actions in `src/quality_os/uitest/generator.py` need selectors that match **your** app's markup. They ship with generic defaults and are marked `ADAPT TO YOUR APP`:

- **`open_row_menu`** — `ROW_SELECTOR`, `MENU_NAME_RE`, `ICON_FALLBACK` (a list row/card and its "…" trigger).
- **`view_file_preview`** — `OUTER_FRAME`, `INNER_FRAME`, `VIEWER_READY` (a document/PDF viewer's iframe chain). If your viewer is a single iframe or inline, simplify or drop the frame locators.

Login discovery and every text/label-based action are resolved automatically and usually need no changes.

## Contributing

Contributions welcome — especially real implementations of the stubbed clients and workflows, more DSL actions, and additional environment adapters. The UI-test generation core is the stable, fully-tested part to build on.
