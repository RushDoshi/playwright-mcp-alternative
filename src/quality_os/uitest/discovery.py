"""discovery.py — AI-free page discovery. Playwright dumps every frame/input/
button on the target page into page_map.json. Cached per URL, so discovery
costs ZERO LLM tokens and runs only once per environment.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

# repo root = .../src/quality_os/uitest/discovery.py -> parents[3]
ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = ROOT / ".cache" / "uitest"


class DiscoveryError(RuntimeError):
    """Page discovery could not produce a page map."""


def _pw_dir() -> Path:
    """Locate the Playwright project (repo-relative, override with PLAYWRIGHT_DIR)."""
    override = os.environ.get("PLAYWRIGHT_DIR")
    candidates = [Path(override)] if override else []
    candidates.append(ROOT / "playwright")
    for p in candidates:
        if (p / "playwright.config.ts").exists():
            return p
    raise DiscoveryError(
        "playwright/playwright.config.ts not found — set PLAYWRIGHT_DIR or run from the repo root")


DISCOVER_SPEC = """\
// AUTO-GENERATED discovery spec — dumps page structure to page_map.json
import { test } from '@playwright/test';
import * as fs from 'fs';

test('DISCOVER page structure @p0', async ({ page }) => {
  test.setTimeout(90_000);
  await page.goto('__URL__', { waitUntil: 'load', timeout: 60_000 });
  await page.waitForTimeout(8_000);

  const map: any = { url: page.url(), title: await page.title(), frames: [] };
  for (const f of page.frames()) {
    const fr: any = { name: f.name(), url: f.url(), inputs: [], buttons: [] };
    const inN = await f.locator('input').count().catch(() => 0);
    for (let i = 0; i < inN; i++) {
      const el = f.locator('input').nth(i);
      fr.inputs.push({
        type: await el.getAttribute('type').catch(() => null),
        name: await el.getAttribute('name').catch(() => null),
        id: await el.getAttribute('id').catch(() => null),
        placeholder: await el.getAttribute('placeholder').catch(() => null),
      });
    }
    const btnSel = 'button, input[type="submit"], a[role="button"]';
    const btN = await f.locator(btnSel).count().catch(() => 0);
    for (let i = 0; i < Math.min(btN, 20); i++) {
      const el = f.locator(btnSel).nth(i);
      fr.buttons.push({
        id: await el.getAttribute('id').catch(() => null),
        type: await el.getAttribute('type').catch(() => null),
        text: (await el.innerText().catch(() => '')).trim().slice(0, 40),
      });
    }
    map.frames.push(fr);
  }
  fs.writeFileSync('__OUT__', JSON.stringify(map, null, 2));
  console.log('DISCOVERY_WRITTEN');
});
"""


def _cache_path(url: str) -> Path:
    h = hashlib.sha1(url.encode()).hexdigest()[:10]
    return CACHE_DIR / f"page_map_{h}.json"


def discover(url: str, log=print, force: bool = False) -> dict:
    """Return the page map for `url`. Uses cache unless force=True."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cp = _cache_path(url)
    if cp.exists() and not force:
        log(f"[DISCOVERY] cache hit → {cp.name} (0 tokens, 0 browser time)")
        return json.loads(cp.read_text(encoding="utf-8"))

    pw = _pw_dir()
    out_json = (pw / "discovery_out.json")
    spec_dir = pw / "suites" / "p0"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = spec_dir / "_discover.spec.ts"
    spec.write_text(
        DISCOVER_SPEC.replace("__URL__", url)
                     .replace("__OUT__", str(out_json).replace("\\", "/")),
        encoding="utf-8")
    log(f"[DISCOVERY] probing {url} …")
    npx = "npx.cmd" if sys.platform == "win32" else "npx"
    res = subprocess.run(
        [npx, "playwright", "test", "p0/_discover.spec.ts", "--reporter=list"],
        cwd=str(pw), capture_output=True, text=True, timeout=180)
    spec.unlink(missing_ok=True)
    if not out_json.exists():
        raise DiscoveryError(
            "discovery produced no page map.\n" + res.stdout[-800:])
    page_map = json.loads(out_json.read_text(encoding="utf-8"))
    out_json.unlink(missing_ok=True)
    cp.write_text(json.dumps(page_map, indent=2), encoding="utf-8")
    log(f"[DISCOVERY] page map cached → {cp.name}")
    return page_map


def compact_map(page_map: dict, max_chars: int = 1500) -> str:
    """Tiny text summary of the page map for the LLM (token control)."""
    lines = [f"PAGE: {page_map.get('title','')} ({page_map.get('url','')})"]
    for i, fr in enumerate(page_map.get("frames", [])):
        ins = [x for x in fr["inputs"] if (x.get("type") or "") != "hidden"]
        if not ins and not fr["buttons"]:
            continue
        lines.append(f"frame[{i}] name={fr.get('name') or '-'}")
        for x in ins:
            lines.append(
                f"  input type={x.get('type')} id={x.get('id')} "
                f"name={x.get('name')} ph={x.get('placeholder')}")
        for b in fr["buttons"][:8]:
            lines.append(f"  button id={b.get('id')} text={b.get('text')}")
    return "\n".join(lines)[:max_chars]


def _id_selector(elem_id: str) -> str:
    """Attribute-selector form — safe even when the id contains spaces or other
    characters that would break the '#id' shorthand (real apps do this; e.g. a
    real-world login field observed with id="Email Address")."""
    return f'[id="{elem_id}"]'


def pick_login_selectors(page_map: dict) -> dict | None:
    """Deterministically choose iframe + email/password/submit selectors.

    Returns {'iframe','email','password','submit'}. 'password'/'submit' are
    None when only an email field is found (alongside some other button, e.g.
    "Continue") but no password field yet — a progressive/multi-step login
    (email -> "Continue" -> password) where the rest only appears after that
    click. The generic action-DSL (click_text/fill_named) drives those later
    steps at run time instead of a pre-discovered selector. Returns None only
    when no login-shaped field is found at all.
    Preference: id > name. Submit prefers non-SSO buttons.
    """
    for idx, fr in enumerate(page_map.get("frames", [])):
        email = pw = submit = None
        for x in fr["inputs"]:
            t = (x.get("type") or "").lower()
            ident = x.get("id") or x.get("name")
            if not ident:
                continue
            sel = _id_selector(x["id"]) if x.get("id") else f"input[name='{x['name']}']"
            if t == "password" and not pw:
                pw = sel
            elif t in ("text", "email") and not email:
                email = sel
            elif t == "submit" and not submit and "sso" not in ident.lower():
                submit = sel
        if not submit:
            for b in fr["buttons"]:
                txt = (b.get("text") or "").lower()
                bid = (b.get("id") or "").lower()
                if "sso" in bid:
                    continue
                if "login" in txt or "sign in" in txt or "login" in bid or (b.get("type") == "submit"):
                    submit = _id_selector(b["id"]) if b.get("id") else None
                    if submit:
                        break
        if email and pw and submit:
            name = fr.get("name")
            iframe = f"iframe[name='{name}']" if (name and idx > 0) else None
            return {"iframe": iframe, "email": email,
                    "password": pw, "submit": submit}
        if email and not pw:
            name = fr.get("name")
            iframe = f"iframe[name='{name}']" if (name and idx > 0) else None
            return {"iframe": iframe, "email": email, "password": None, "submit": None}
    return None
