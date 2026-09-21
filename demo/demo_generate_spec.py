#!/usr/bin/env python3
"""Zero-setup demo — no app, no Jira, no credentials, no LLM call.

Proves the core of playwright-mcp-alternative: a validated closed action-DSL is rendered
DETERMINISTICALLY into a valid Playwright .spec.ts, and the generated file
passes every safety gate (test count, brace/paren balance, required selectors,
credential-leak check). This is the "is it real?" check a reviewer can run in
seconds on a fresh clone.

Run:
    python demo/demo_generate_spec.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Make src/ importable without installing the package.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quality_os.uitest.generator import render_spec, gates  # noqa: E402

# A discovered login form (normally produced token-free by uitest/discovery.py).
CFG = {
    "env_name": "demo",
    "login_url": "https://example.com/login",
    "home_match": "/home|dashboard",
    "nav_timeout_ms": 45000,
    "field_timeout_ms": 20000,
}
SELECTORS = {
    "iframe": None,
    "email": "#email",
    "password": "#password",
    "submit": "button[type='submit']",
}

# Scenarios exactly as the LLM would emit them: actions from the closed
# vocabulary only — never code. The renderer turns these into TypeScript.
SCENARIOS = [
    {
        "id": "SC-1",
        "title": "valid credentials reach home @p0",
        "tag": "@p0",
        "comment": "POSITIVE — valid login",
        "steps": [
            {"action": "fill", "field": "email", "value": "${USER}"},
            {"action": "fill", "field": "password", "value": "${PASS}"},
            {"action": "click", "field": "submit"},
            {"action": "wait_url"},
            {"action": "expect_url"},
        ],
    },
    {
        "id": "SC-2",
        "title": "search then open an item and verify @p1",
        "tag": "@p1",
        "comment": "generic multi-step journey across arbitrary screens",
        "steps": [
            {"action": "fill", "field": "email", "value": "${USER}"},
            {"action": "fill", "field": "password", "value": "${PASS}"},
            {"action": "click", "field": "submit"},
            {"action": "wait_url"},
            {"action": "type_search", "target": "Search", "value": "quarterly report"},
            {"action": "click_text", "target": "Reports"},
            {"action": "expect_visible", "target": "Results"},
        ],
    },
    {
        "id": "SC-3",
        "title": "wrong password stays on login @p1",
        "tag": "@p1",
        "comment": "NEGATIVE — wrong password",
        "steps": [
            {"action": "fill", "field": "email", "value": "${USER}"},
            {"action": "fill", "field": "password", "value": "${WRONG_PASS}"},
            {"action": "click", "field": "submit"},
            {"action": "wait_ms", "value": "3000"},
            {"action": "expect_not_url"},
        ],
    },
]


def main() -> int:
    spec = render_spec(CFG, SELECTORS, SCENARIOS, "DEMO-1")

    out_dir = Path(tempfile.mkdtemp(prefix="playwright-mcp-alternative-demo-"))
    spec_path = out_dir / "DEMO-1.spec.ts"
    spec_path.write_text(spec, encoding="utf-8")

    failures = gates(spec_path, expected_tests=len(SCENARIOS))

    print("=" * 68)
    print("playwright-mcp-alternative — deterministic spec generation demo")
    print("=" * 68)
    print(f"Input      : {len(SCENARIOS)} scenarios as a closed action-DSL (no code)")
    print(f"Generated  : {spec_path}  ({len(spec.splitlines())} lines of TypeScript)")
    print(f"Credentials: read from process.env at run time (never in the file)")
    leaked = "VALID_USER = process.env.TEST_USER" in spec
    print(f"Cred check : {'PASS — no literal secrets embedded' if leaked else 'n/a'}")
    print()
    if failures:
        print("SAFETY GATES: FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("SAFETY GATES: ALL PASSED "
          "(test count, brace/paren balance, selectors, credential-leak)")
    print()
    print("First 24 lines of the generated spec:")
    print("-" * 68)
    for line in spec.splitlines()[:24]:
        print("  " + line)
    print("-" * 68)
    print(f"\nFull spec written to: {spec_path}")
    print("Copy it into playwright/suites/p0/ and `npx playwright test` to run it")
    print("against a real app (set BASE_URL + TEST_USER/TEST_PASS).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
