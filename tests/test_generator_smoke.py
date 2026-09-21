"""Smoke test — deterministic spec generation + safety gates, no external deps.

Runs on a fresh clone with only the base requirements installed:
    pip install -r requirements.txt && pytest
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quality_os.uitest.generator import render_spec, gates  # noqa: E402
from quality_os.uitest.designer import _sanitize  # noqa: E402

CFG = {"env_name": "test", "login_url": "https://example.com/login",
       "home_match": "/home|dashboard", "nav_timeout_ms": 45000, "field_timeout_ms": 20000}
SELECTORS = {"iframe": None, "email": "#email", "password": "#password",
             "submit": "button[type='submit']"}


def _scenarios():
    return [
        {"id": "SC-1", "title": "login @p0", "tag": "@p0", "comment": "positive",
         "steps": [{"action": "fill", "field": "email", "value": "${USER}"},
                   {"action": "fill", "field": "password", "value": "${PASS}"},
                   {"action": "click", "field": "submit"},
                   {"action": "wait_url"}, {"action": "expect_url"}]},
        {"id": "SC-2", "title": "journey @p1", "tag": "@p1", "comment": "generic",
         "steps": [{"action": "click_text", "target": "Reports"},
                   {"action": "expect_visible", "target": "Results"}]},
    ]


def test_generated_spec_passes_all_gates():
    spec = render_spec(CFG, SELECTORS, _scenarios(), "TEST-1")
    p = Path(tempfile.mkdtemp()) / "TEST-1.spec.ts"
    p.write_text(spec, encoding="utf-8")
    assert gates(p, expected_tests=2) == []


def test_no_credentials_are_embedded():
    spec = render_spec(CFG, SELECTORS, _scenarios(), "TEST-1")
    assert "process.env.TEST_USER" in spec
    assert "process.env.TEST_PASS" in spec
    # the real password value must never appear as a literal
    assert "hunter2" not in spec


def test_sanitizer_drops_unknown_actions():
    payload = {"scenarios": [{"id": "X", "title": "t", "tag": "@p1", "steps": [
        {"action": "click_text", "target": "OK"},
        {"action": "run_arbitrary_code", "value": "rm -rf /"},  # not in vocabulary
    ]}]}
    out = _sanitize(payload, "TEST-1")
    actions = [s["action"] for sc in out for s in sc["steps"]]
    assert "run_arbitrary_code" not in actions
    assert "click_text" in actions
