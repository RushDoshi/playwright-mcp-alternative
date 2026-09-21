"""generator.py — deterministic DSL → TypeScript renderer + safety gates.

The LLM never writes code. This module maps validated DSL actions onto fixed
code templates using the DISCOVERED selectors and the ENV config, so the output
always compiles and the credentials/URLs are fully dynamic.

Two action families (see ``uitest/designer.py``):
  * login actions (fill/click/...) address the pre-discovered login selectors.
  * generic actions (click_text/fill_named/upload_file/download/...) address
    anything else in the app by visible text/label. They resolve to resilient
    Playwright role/text locators generated once as helper functions, never to
    arbitrary code — a step can only ever become one of the fixed templates
    below, with its text safely escaped into a string literal.
"""
from __future__ import annotations

import re
from pathlib import Path

_NOW_PLACEHOLDER = "${NOW}"

# Resolved by a JS Date() call inside the generated test, not baked in as a
# Python-computed literal at spec-generation time — otherwise re-running the
# same already-generated .spec.ts a second time would reuse the exact same
# frozen name (e.g. a workspace name) instead of getting a fresh one.
_NOW_TOKEN_HELPER = """
function nowToken() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}
"""

# Every generic action prefers a match inside an open dialog over one on the
# page behind it (a dialog's own submit button, e.g. "Create Workspace", can
# share a name with the page-level trigger that opened it) — but the preference
# is decided per-lookup, by whether the dialog-scoped locator actually has a
# match right now, never by a separate "is a dialog open" check. That upfront
# check broke repeatedly in practice: a closed dialog can stay in the DOM
# (hidden, or an empty overlay-host container) and still pass isVisible(), an
# unrelated toast/autocomplete panel can also carry role="dialog", and a real
# dialog can report visible for an instant mid-close-animation. Within
# whichever scope wins, .first() is still used — ordinary pages can have
# several same-role matches too (e.g. multiple "Open App" tiles on a
# dashboard), and picking the first one there is the correct, original
# behavior; only the dialog-vs-page choice needed to change.
_DIALOG_HELPER = """
function dlg(ctx) { return ctx.getByRole('dialog'); }

async function pickScoped(dialogLocator, pageLocator) {
  if (await dialogLocator.count() > 0) return dialogLocator.last();
  return pageLocator.first();
}
"""

_CLICK_TEXT_HELPER = """
async function clickByText(ctx, text, opts = {}) {
  // A file/folder row's name is often plain text, not an interactive role —
  // some apps open its preview only on a double-click, never a single one
  // (confirmed live: a single click on a filename left the list view exactly
  // as-is). opts.dblclick swaps click() for dblclick() at every resolution
  // point below without duplicating the whole scoping logic in a second
  // near-identical helper.
  const method = opts.dblclick ? 'dblclick' : 'click';
  // Exact match is tried before substring — a same-page overlay/panel that
  // isn't a real ARIA dialog (so dialog-scoping can't help) can still have a
  // button whose exact name is a substring of another already-visible one
  // (e.g. a panel's own "Upload" submit next to a page's "Upload Files"
  // trigger) — substring matching alone can't tell them apart.
  for (const exact of [true, false]) {
    // menuitem: a row's "..." dropdown (e.g. Archive/Restore options) isn't a
    // real ARIA dialog, so dialog-scoping can't help there either -- without
    // an exact-match attempt, "Archive" falls through to substring matching
    // and can hit an unrelated same-page label like "Archived" nav item.
    const dialogRole = dlg(ctx).getByRole('button', { name: text, exact })
      .or(dlg(ctx).getByRole('link', { name: text, exact }))
      .or(dlg(ctx).getByRole('menuitem', { name: text, exact }));
    const pageRole = ctx.getByRole('button', { name: text, exact })
      .or(ctx.getByRole('link', { name: text, exact }))
      .or(ctx.getByRole('menuitem', { name: text, exact }));
    // .count() is an immediate, non-retrying snapshot -- right after a state
    // change that makes a new role match appear a beat later (e.g. checking a
    // row so a "Move" toolbar button renders), it reads 0 before the app has
    // re-rendered, and this loop then falls through to the substring branch
    // below (observed in practice: a "Move" button count read 0
    // 5ms after checking Select-all, so the substring match ran instead and
    // hit the unrelated "Automation for Move Files" workspace-switcher
    // combobox, which contains "Move" as a substring and sits earlier in the
    // DOM, so .first() grabbed it and the scenario hung on the next step).
    // Poll briefly so a slightly-delayed role match still wins over the
    // riskier substring fallback; exits immediately once already present.
    const deadline = Date.now() + 3000;
    let roleCount = (await dialogRole.count()) + (await pageRole.count());
    while (roleCount === 0 && Date.now() < deadline) {
      await new Promise(r => setTimeout(r, 150));
      roleCount = (await dialogRole.count()) + (await pageRole.count());
    }
    if (roleCount > 0) {
      await (await pickScoped(dialogRole, pageRole))[method]();
      await new Promise(r => setTimeout(r, 300));
      return;
    }
  }
  await (await pickScoped(dlg(ctx).getByText(text, { exact: false }),
                          ctx.getByText(text, { exact: false })))[method]();
  await new Promise(r => setTimeout(r, 300));   // let a resulting navigation/dialog settle
}

async function clickByTextIfPresent(ctx, text) {
  // Some journey steps only apply conditionally on prior state (e.g. a
  // "Clear Selection" control that's only on screen if an earlier step left
  // something selected) -- unlike clickByText, this one is expected to
  // sometimes have nothing to click, so it checks existence first and does
  // nothing rather than hanging until the test timeout waiting for text that
  // will never appear (observed in practice: the prior step's
  // move already cleared the selection, so "Clear Selection" never rendered).
  const present =
    (await dlg(ctx).getByText(text, { exact: false }).count()) +
    (await ctx.getByText(text, { exact: false }).count()) > 0;
  if (!present) return;
  await clickByText(ctx, text);
}
"""

_SWATCH_HELPER = """
async function selectFirstSwatch(ctx, kind) {
  // Unlabeled visual pickers (icon/color/avatar swatches) commonly have no ARIA
  // role or accessible name at all, so text/role locators can't reach them. The
  // kind -> pattern mapping is fixed here (never LLM-authored CSS) and only
  // covers a closed set of known swatch shapes.
  // :not(:has(*)) excludes wrapper/container elements that also match by
  // class name but aren't themselves a clickable swatch — only leaf elements
  // are real options. Scoped to the
  // dialog only (never the page) — a swatch picker only exists while its
  // dialog is open, so there's no dialog-vs-page ambiguity to resolve here.
  // First matching visible candidate, in DOM order — later matches can be
  // unrelated same-class icons elsewhere in the dialog (e.g. a close/caret
  // glyph) that pass the broad class pattern but aren't real picker options.
  const PATTERNS = {
    icon: 'i[class]:not([class*="placeholder" i]):not(:has(*)), '
        + '[class*="icon" i]:not([class*="placeholder" i]):not(:has(*))',
    color: '[class*="color" i]:not([class*="placeholder" i]):not(:has(*)), '
         + '[class*="swatch" i]:not(:has(*))',
    avatar: '[class*="avatar" i]:not([class*="placeholder" i]):not(:has(*))',
  };
  const sel = PATTERNS[kind];
  if (!sel) return;
  const candidates = dlg(ctx).locator(sel);
  const count = await candidates.count();
  for (let i = 0; i < count; i++) {
    const el = candidates.nth(i);
    if (await el.isVisible().catch(() => false)) {
      await el.click();
      return;
    }
  }
}
"""

_FILL_NAMED_HELPER = """
async function namedField(ctx, label) {
  const inDialog = dlg(ctx).getByLabel(label).or(dlg(ctx).getByPlaceholder(label))
    .or(dlg(ctx).getByRole('textbox', { name: label }));
  const onPage = ctx.getByLabel(label).or(ctx.getByPlaceholder(label))
    .or(ctx.getByRole('textbox', { name: label }));
  return pickScoped(inDialog, onPage);
}

async function fillNamed(ctx, label, value) {
  await (await namedField(ctx, label)).fill(value);
}
"""

_SELECT_ALL_HELPER = """
async function checkSelectAll(ctx, target) {
  const dNamed = dlg(ctx).getByRole('checkbox', { name: target });
  const pNamed = ctx.getByRole('checkbox', { name: target });
  if ((await dNamed.count()) + (await pNamed.count()) > 0) {
    await (await pickScoped(dNamed, pNamed)).check();
    return;
  }
  // A bare header "select all" checkbox commonly has no accessible name at
  // all (no label, no aria-label) — fall back to the first checkbox in scope.
  await (await pickScoped(dlg(ctx).getByRole('checkbox'), ctx.getByRole('checkbox'))).check();
}
"""

_ROW_MENU_HELPER = """
async function openRowMenu(ctx, rowText) {
  // A row's "..." options trigger is either a named button
  // (role="button" with an accessible name like "Options"/"Actions") or
  // an unlabeled icon button -- scoped to the specific row containing rowText,
  // since every row's trigger shares the same name (or no name at all), unlike
  // a page-unique control clickByText can already resolve.
  //
  // ADAPT TO YOUR APP: ROW_SELECTOR is the repeated container that wraps one
  // list row/card, and MENU_NAME(S)/ICON_FALLBACK are how that row's options
  // trigger is identified. The defaults below are conventional guesses
  // ([role="listitem"], an "Options"/"Actions" button, or an icon button
  // whose class contains "more"); change them to match your own markup.
  const ROW_SELECTOR = '[role="listitem"], li, tr';
  const MENU_NAME_RE = /more options|options|actions|menu/i;
  const ICON_FALLBACK = 'button[class*="more" i], [aria-haspopup="menu"]';
  const row = ctx.locator(ROW_SELECTOR).filter({ hasText: rowText }).last();
  // .count() is an immediate, non-retrying snapshot -- if the row hasn't
  // rendered yet (e.g. right after a search), it reads 0 regardless of which
  // trigger the row will actually have, so a false negative here hangs forever
  // waiting on the wrong branch. Wait for the row itself first.
  await row.waitFor({ state: 'visible', timeout: 15000 }).catch(() => {});
  const named = row.getByRole('button', { name: MENU_NAME_RE });
  if (await named.count() > 0) {
    await named.first().click();
  } else {
    await row.locator(ICON_FALLBACK).first().click();
  }
  await new Promise(r => setTimeout(r, 300));
}
"""

_DROPDOWN_HELPER = """
async function selectDropdownOption(ctx, optionText) {
  // Open the combobox only if its option list isn't already showing.
  if (await ctx.getByRole('option', { name: optionText }).count() === 0) {
    await ctx.getByRole('combobox').first().click();
    await new Promise(r => setTimeout(r, 300));
  }
  await ctx.getByRole('option', { name: optionText }).click();
  await new Promise(r => setTimeout(r, 300));
}
"""

_CAPTURE_FILES_HELPER = """
async function captureCheckedNames(ctx) {
  // A file/folder listing's row checkboxes are commonly each named by the
  // filename itself (observed in practice: an accessibility snapshot showed
  // each row as e.g. `checkbox "<filename>"`, alongside a `checkbox
  // "select-all"` header control) -- ariaSnapshot() is Playwright's own
  // supported way to read the computed accessible name without hardcoding an
  // app-specific DOM attribute/class. This lets a scenario verify against
  // whatever files ACTUALLY exist in a folder at runtime instead of names
  // hardcoded from ticket text, which can silently drift out of sync with the
  // real environment (observed in practice: a ticket named one file that the
  // real folder didn't actually contain, while every other named file matched).
  const parseNames = (snap) => {
    const names = [];
    const re = /checkbox "([^"]+)"/g;
    let m;
    while ((m = re.exec(snap))) {
      const n = m[1].trim();
      if (n.toLowerCase() !== 'select-all' && n.toLowerCase() !== 'select all') names.push(n);
    }
    return names;
  };
  // A freshly-opened folder's file list loads asynchronously (a server fetch,
  // not just a client-side render) -- ariaSnapshot() taken immediately after
  // navigating in can catch the list before any row has arrived, returning []
  // regardless of what the folder actually contains (observed in practice:
  // capturing right after opening a folder that in fact held several files
  // returned an empty baseline, which then made every real file in it
  // look like an unexpected extra later in the same scenario). Poll briefly
  // for at least one row before settling -- a folder still empty after that
  // keeps returning [] exactly as before.
  const deadline = Date.now() + 4000;
  let names = parseNames(await ctx.locator('body').ariaSnapshot());
  while (names.length === 0 && Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 250));
    names = parseNames(await ctx.locator('body').ariaSnapshot());
  }
  return names;
}
"""

_DEBUG_LOG_HELPER = """
function attachPageDebugLog(page) {
  // Whatever step fails next, this was already running from the top of the
  // test — reconstructing "what was the app actually doing right before this
  // broke" from a trace alone after the fact is slow and easy to get wrong
  // (this took real back-and-forth on live failures before this existed).
  // Capped so a chatty page can't blow up report size on a long scenario.
  const log: string[] = [];
  const push = (line: string) => { log.push(line); if (log.length > 400) log.shift(); };
  page.on('console', m => push(`[console:${m.type()}] ${m.text()}`));
  page.on('pageerror', e => push(`[pageerror] ${e.message}`));
  page.on('requestfailed', r => push(`[requestfailed] ${r.method()} ${r.url()} - ${r.failure()?.errorText ?? 'unknown'}`));
  page.on('response', r => {
    if (r.status() < 400) return;
    // The status line alone ("600", "500"...) says something broke but not
    // what — an app-level upload/save error response body usually carries
    // the actual reason (quota, validation, session), which is the part
    // worth having in the report without re-fetching a trace to find it.
    r.text().then(
      body => push(`[http ${r.status()}] ${r.request().method()} ${r.url()} - body: ${body.slice(0, 500)}`),
      () => push(`[http ${r.status()}] ${r.request().method()} ${r.url()} - body: (unreadable)`),
    );
  });
  return log;
}
"""


def normalize_progressive_login(scenarios: list[dict], selectors: dict, cfg: dict) -> list[dict]:
    """Deterministically rewrite a fill/password or click/submit DSL step into
    its generic (fill_named/click_text) equivalent whenever that selector
    wasn't actually discovered on the page (a progressive login -- see
    config/environments.yaml's per-environment ``progressive_login`` block:
    {continue_text, password_label, submit_text}).

    Never trusts an LLM call to consistently choose the safe generic action on
    every single run -- confirmed live, it silently reverted to the fixed
    field-based DSL despite the designer persona explicitly saying not to,
    which renders as ``ctx.locator('').fill(...)`` and crashed several real
    Playwright browsers in practice before this existed. A no-op
    wherever both selectors were actually discovered, or the environment has
    no ``progressive_login`` config at all (render_spec()'s selector gate is
    still the safety net in that case).
    """
    progressive = cfg.get("progressive_login") or {}
    if not progressive:
        return scenarios
    out = []
    for sc in scenarios:
        new_steps = []
        for st in sc["steps"]:
            a, field = st.get("action"), st.get("field")
            new_st = st
            if (a == "fill" and field == "password" and not selectors.get("password")
                    and progressive.get("password_label")):
                new_st = {k: v for k, v in st.items() if k not in ("action", "field")}
                new_st["action"] = "fill_named"
                new_st["target"] = progressive["password_label"]
            elif (a == "click" and field == "submit" and not selectors.get("submit")
                  and progressive.get("submit_text")):
                new_st = {k: v for k, v in st.items() if k not in ("action", "field", "value")}
                new_st["action"] = "click_text"
                new_st["target"] = progressive["submit_text"]
            new_steps.append(new_st)
        out.append({**sc, "steps": new_steps})
    return out


def _value_expr(text: str) -> str:
    """Turn free text into a JS expression: an escaped string literal, or —
    when it contains ${NOW} — a concatenation with the runtime-computed `NOW`
    variable (`'Automation ' + NOW`) instead of a literal baked-in timestamp.
    """
    if not text or _NOW_PLACEHOLDER not in text:
        return _js_string(text)
    parts = text.split(_NOW_PLACEHOLDER)
    return " + NOW + ".join(_js_string(p) for p in parts)


def _js_ident(text: str) -> str:
    """Turn free text (a capture_files/expect_same_files `target`) into a safe,
    stable JS variable name -- same input always maps to the same identifier,
    which is what lets a later expect_same_files step reference an earlier
    capture_files step's variable by matching `target` text."""
    ident = re.sub(r"[^A-Za-z0-9_]", "_", text.strip()) or "files"
    if ident[0].isdigit():
        ident = "_" + ident
    return f"captured_{ident}"


def _js_string(text: str, max_len: int = 200) -> str:
    """Escape arbitrary text into a safe single-quoted JS/TS string literal.

    This is what keeps free-text ticket content (workspace names, invite emails,
    button labels) safe to embed: it is always treated as a string value, never
    as code, no matter what characters the LLM or the ticket contains.
    """
    text = str(text)[:max_len]
    text = text.replace("\\", "\\\\").replace("'", "\\'")
    text = text.replace("\r", " ").replace("\n", " ")
    return f"'{text}'"


def render_spec(cfg: dict, selectors: dict, scenarios: list[dict], ticket_key: str,
                attachments: dict[str, str] | None = None) -> str:
    attachments = attachments or {}

    iframe = selectors.get("iframe")
    ctx_init = (f"  const ctx = page.frameLocator({iframe!r});"
                if iframe else "  const ctx = page;")

    used_actions = {st["action"] for sc in scenarios for st in sc["steps"]}
    needs_now = any(_NOW_PLACEHOLDER in (st.get("target", "") + st.get("value", ""))
                    for sc in scenarios for st in sc["steps"])
    needs_download = bool({"download", "expect_zip_contains"} & used_actions)
    needs_zip = "expect_zip_contains" in used_actions
    needs_click_text = bool({"click_text", "dblclick_text", "click_text_optional",
                             "view_file_preview", "download"} & used_actions)
    needs_fill_named = bool({"fill_named", "type_search", "add_list_item"} & used_actions)
    needs_swatch = "select_first_swatch" in used_actions
    needs_select_all = "select_all" in used_actions
    needs_row_menu = "open_row_menu" in used_actions
    needs_dropdown = "select_dropdown_option" in used_actions
    needs_capture = bool({"capture_files", "expect_same_files"} & used_actions)
    needs_scope = bool({"click_text", "dblclick_text", "click_text_optional",
                        "view_file_preview", "download",
                        "fill_named", "type_search", "add_list_item", "expect_visible",
                        "select_all", "select_first_swatch", "check_named"} & used_actions)

    lines: list[str] = [
        "/**",
        f" * AI-Generated Playwright Spec — {ticket_key}",
        f" * Environment : {cfg['env_name']}   (config/environments.yaml)",
        f" * Discovered  : iframe={selectors.get('iframe')} email={selectors['email']}",
        f" *               password={selectors['password']} submit={selectors['submit']}",
        " * Credentials : read from process.env.TEST_USER / TEST_PASS at run time",
        " *               (never embedded as literals — this file is safe to commit)",
        " */",
        "import { test, expect } from '@playwright/test';",
    ]
    if needs_download:
        lines += ["import * as fs from 'fs';", "import * as path from 'path';",
                  "import * as os from 'os';"]
    if needs_zip:
        lines += ["import AdmZip from 'adm-zip';"]

    lines += [
        "",
        f"const LOGIN_URL  = '{cfg['login_url']}';",
        f"const HOME_MATCH = /{cfg['home_match'].replace('/', chr(92) + '/')}/i;",
        "const VALID_USER = process.env.TEST_USER ?? '';",
        "const VALID_PASS = process.env.TEST_PASS ?? '';",
        "const WRONG_PASS = 'WrongPassword999!';",
        "",
        f"const SEL_EMAIL    = '{selectors.get('email') or ''}';",
        f"const SEL_PASSWORD = '{selectors.get('password') or ''}';",
        f"const SEL_SUBMIT   = '{selectors.get('submit') or ''}';",
    ]
    if needs_download:
        lines.append("const DOWNLOADS_DIR = path.join(os.homedir(), 'Downloads');")

    lines += [
        "",
        "async function openPage(page) {",
        f"  await page.goto(LOGIN_URL, {{ waitUntil: 'load', timeout: {cfg['nav_timeout_ms']} }});",
        ctx_init,
        f"  await ctx.locator(SEL_EMAIL).waitFor({{ state: 'visible', timeout: {cfg['field_timeout_ms']} }});",
        "  return ctx;",
        "}",
    ]
    if needs_now:
        lines.append(_NOW_TOKEN_HELPER)
    if needs_scope:
        lines.append(_DIALOG_HELPER)
    if needs_click_text:
        lines.append(_CLICK_TEXT_HELPER)
    if needs_fill_named:
        lines.append(_FILL_NAMED_HELPER)
    if needs_swatch:
        lines.append(_SWATCH_HELPER)
    if needs_select_all:
        lines.append(_SELECT_ALL_HELPER)
    if needs_row_menu:
        lines.append(_ROW_MENU_HELPER)
    if needs_dropdown:
        lines.append(_DROPDOWN_HELPER)
    if needs_capture:
        lines.append(_CAPTURE_FILES_HELPER)
    lines.append(_DEBUG_LOG_HELPER)

    header = "\n".join(lines) + "\n"

    field_sel = {"email": "SEL_EMAIL", "password": "SEL_PASSWORD", "submit": "SEL_SUBMIT"}
    value_map = {"${USER}": "VALID_USER", "${PASS}": "VALID_PASS", "${WRONG_PASS}": "WRONG_PASS"}

    def step_lines(st: dict, counters: dict) -> list[str]:
        a = st["action"]
        if a == "fill":
            v = value_map.get(st["value"])
            expr = v if v else _js_string(st["value"])
            return [f"  await ctx.locator({field_sel[st['field']]}).fill({expr});"]
        if a == "click":
            return [f"  await ctx.locator({field_sel[st['field']]}).click();"]
        if a == "wait_url":
            return ["  await page.waitForURL(HOME_MATCH, { timeout: 30000 });"]
        if a == "expect_url":
            return ["  await expect(page).toHaveURL(HOME_MATCH);"]
        if a == "expect_not_url":
            return ["  await expect(page).not.toHaveURL(HOME_MATCH);"]
        if a == "wait_ms":
            ms = "".join(c for c in st["value"] if c.isdigit()) or "3000"
            return [f"  await page.waitForTimeout({min(int(ms), 15000)});"]

        # ---- generic actions ----
        target = _value_expr(st.get("target", ""))
        # An exact credential placeholder resolves to the injected JS constant
        # (never a literal), same as the legacy 'fill' action above — otherwise
        # it's free text, with ${NOW} (if present) resolved at test-run time via
        # _value_expr, not baked in here. Without the credential check, "${PASS}"
        # would render as the literal four characters, never the real password.
        raw_value = st.get("value", "")
        value = ((value_map.get(raw_value) or _value_expr(raw_value))
                if raw_value else None)

        if a == "click_text":
            return [f"  await clickByText(ctx, {target});"]
        if a == "dblclick_text":
            return [f"  await clickByText(ctx, {target}, {{ dblclick: true }});"]
        if a == "click_text_optional":
            return [f"  await clickByTextIfPresent(ctx, {target});"]
        if a == "fill_named":
            return [f"  await fillNamed(ctx, {target}, {value});"]
        if a == "expect_visible":
            return [
                f"  await expect(await pickScoped(dlg(ctx).getByText({target}, {{ exact: false }}), "
                f"ctx.getByText({target}, {{ exact: false }}))).toBeVisible({{ timeout: 20000 }});",
            ]
        if a == "view_file_preview":
            # The document viewer opens in a NEW browser tab, not inline (confirmed
            # live) — so the check can't run on ctx/page, only on the popup
            # page Playwright's own 'page' context event hands back. That page
            # only exists as a local variable for the duration of this one
            # step, which is why open+check+close is one atomic action rather
            # than three separate DSL steps: nothing carries previewPage across
            # step boundaries in this renderer.
            #
            # Closed directly via previewPage.close() rather than clicking a
            # UI "Close" control — reliably returns to the original tab
            # regardless of what that control does internally, and needs no
            # assumption about where it lives on a page whose layout this
            # renderer never discovered.
            #
            # Single click, not double: the filename is a real hyperlink, one
            # click opens its preview.
            #
            # NOTE ON NESTED IFRAMES: many document/PDF viewers render the
            # actual page canvas inside a viewer iframe that is itself nested
            # one or more iframes deep in the preview tab (e.g. an outer
            # container iframe whose document then hosts the real viewer
            # iframe). A frameLocator() built directly against the inner viewer
            # therefore matches zero elements and never resolves, regardless of
            # timeout length -- that is a selector-DEPTH problem, not a
            # rendering-speed one, and no amount of extra wait fixes it.
            #
            # ADAPT TO YOUR APP: set OUTER_FRAME / INNER_FRAME / VIEWER_READY
            # below to your viewer's own iframe chain and a "page rendered"
            # locator. If your viewer is a single iframe (or inline), drop the
            # outer .frameLocator() call. Benign, unrelated console errors from
            # the opener are common here and do not indicate the file failed to
            # render -- diagnose from whether VIEWER_READY becomes visible, not
            # from console noise.
            #
            # .last() (rather than a hardcoded index) is a defensive fallback in
            # case a flow ends up with more than one viewer instance nested
            # inside the outer frame.
            #
            # Console/pageerror/close listeners are debug evidence for *why*
            # the wait failed, attached to the report only on failure.
            #
            # bringToFront() immediately on open so a real render is not
            # confused with a backgrounded/throttled one. 20000ms is generous
            # for a real render (observed complete in well under 10s live);
            # a failure here means the selector or DOM structure changed
            # again, not that the file needs longer to load.
            return [
                "  {",
                "    const [previewPage] = await Promise.all([",
                "      page.context().waitForEvent('page'),",
                f"      clickByText(ctx, {target}),",
                "    ]);",
                "    await previewPage.bringToFront();",
                "    const viewerLog: string[] = [];",
                "    previewPage.on('console', m => viewerLog.push(`[console:${m.type()}] ${m.text()}`));",
                "    previewPage.on('pageerror', e => viewerLog.push(`[pageerror] ${e.message}`));",
                "    previewPage.on('close', () => viewerLog.push('[event] preview tab closed itself'));",
                "    await previewPage.waitForLoadState('load').catch(() => {});",
                "    try {",
                # ADAPT TO YOUR APP (see note above): OUTER_FRAME / INNER_FRAME
                # are the viewer's iframe chain; VIEWER_READY is a locator that
                # is visible only once a page has actually rendered. For a
                # single-iframe viewer, use one .frameLocator(); for an inline
                # viewer, drop them and locate on previewPage directly.
                "      const OUTER_FRAME = 'iframe[id*=\"view\" i], iframe[class*=\"viewer\" i]';",
                "      const INNER_FRAME = 'iframe[id*=\"viewer\" i], iframe[class*=\"viewer\" i]';",
                "      const VIEWER_READY = '[class*=\"page\" i], canvas, .textLayer';",
                "      await expect(previewPage.frameLocator(OUTER_FRAME).frameLocator(INNER_FRAME).last().locator(VIEWER_READY).first())",
                "        .toBeVisible({ timeout: 20000 });",
                "    } catch (e) {",
                "      await test.info().attach('view-file-preview-debug.log', "
                "{ body: viewerLog.join('\\n') || '(no console/pageerror/close events captured)', "
                "contentType: 'text/plain' });",
                "      throw e;",
                "    }",
                "    if (!previewPage.isClosed()) await previewPage.close();",
                "  }",
            ]
        if a == "annotate":
            # Two mechanisms, not one, because neither reporter honors the other's:
            # test.info().annotations gets its own "Annotations" panel in Playwright's
            # native HTML report, but allure-playwright silently drops any annotation
            # whose type isn't 'issue'/'tms'/'description' — it never reaches the
            # Allure report at all. An empty test.step() carrying the value in its
            # name is the one mechanism both reporters record unconditionally, so it
            # covers Allure; the annotation push stays for Playwright's own panel.
            return [
                f"  test.info().annotations.push({{ type: {target}, description: {value} }});",
                f"  await test.step(`${{{target}}}: ${{{value}}}`, async () => {{}});",
            ]
        if a == "capture_files":
            # Assignment, not `const` -- the variable is declared once at the
            # top of the test body (see scenario_capture_vars below) so it's
            # still in scope from a LATER, separate test.step() callback (each
            # test.step() body is its own closure; a `const` declared inside
            # one is invisible to another).
            var = _js_ident(st.get("target", "files"))
            return [f"  {var} = await captureCheckedNames(ctx);"]
        if a == "expect_same_files":
            var = _js_ident(st.get("target", "files"))
            return [
                "  {",
                "    const nowFiles = await captureCheckedNames(ctx);",
                f"    const missing = {var}.filter(f => !nowFiles.includes(f));",
                f"    const extra = nowFiles.filter(f => !{var}.includes(f));",
                "    expect(missing, `missing file(s): ${missing.join(', ')}`).toEqual([]);",
                "    expect(extra, `unexpected extra file(s): ${extra.join(', ')}`).toEqual([]);",
                "  }",
            ]
        if a == "capture_screenshot":
            # Attached via test.info(), not written to disk under a fixed path —
            # this is the one action a reviewer should be able to trust *whether
            # the run passed or failed*, so it must show up the same way in both
            # cases. The built-in `screenshot: 'only-on-failure'` config option
            # only ever fires once, at the very end of a FAILED test — it cannot
            # give a passing run any visual evidence at all, which is exactly
            # what left a real reviewer unable to confirm a "Move files between
            # folders" scenario had actually moved anything (observed in
            # practice: the HTML/Allure report for a green run carried no
            # image whatsoever). Always full-page: a folder listing can be
            # taller than the viewport, and the point is to show the state that
            # was just asserted, not just whatever was in the fold.
            return [
                f"  await test.info().attach({target}, "
                "{ body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });",
            ]
        if a in ("type_search", "add_list_item"):
            return [
                "  {",
                f"    const f = await namedField(ctx, {target});",
                f"    await f.fill({value});",
                "    await f.press('Enter');",
                # A search/add can trigger an async navigation or re-render that
                # doesn't complete instantly — without settling here, it can land
                # later and clobber whatever the *next* action just did (seen live:
                # a delayed search navigation overwrote an upload panel that had
                # already opened correctly).
                "    await new Promise(r => setTimeout(r, 500));",
                "  }",
            ]
        if a == "select_first_swatch":
            kind = st.get("target", "")
            return [f"  await selectFirstSwatch(ctx, {_js_string(kind)});"]
        if a == "reload_page":
            return [
                "  await page.reload({ waitUntil: 'load' });",
                # 'load' fires before an SPA client has finished bootstrapping/
                # rendering — the next interaction (e.g. a button that briefly
                # shows a loading spinner) needs a moment beyond that.
                "  await new Promise(r => setTimeout(r, 4000));",
            ]
        if a == "upload_file":
            names = [n.strip() for n in st.get("value", "").split(",") if n.strip()]
            paths = [attachments.get(n) for n in names]
            if not names or any(p is None for p in paths):
                return []  # attachment not staged — skip rather than emit a bad path
            # One setInputFiles() call per file, not a single call with an array —
            # a file input without the 'multiple' attribute rejects a multi-file
            # array outright ("Non-multiple file input can only accept single
            # file"), and this way works for both single- and multi-file inputs.
            # .last(), not .first(): a page can have more than one hidden
            # input[type=file] for unrelated features (confirmed live — a second,
            # earlier one belonged to an entirely different upload flow); the one
            # tied to whatever panel was just opened tends to be the most
            # recently-added to the DOM. The same locator is reused across calls
            # rather than re-triggering the upload panel's own picker button
            # between files, which was observed to drop the earlier selection.
            fill_input = "ctx.locator('input[type=\"file\"]').last()"
            lines_out = []
            for p, nm in zip(paths, names):
                fname_js = _js_string(nm)
                # Self-verifying, not fire-and-forget: setInputFiles() on this
                # widget was observed to silently do nothing on some attempts
                # (no error, no exception — just no visible effect), so success
                # is confirmed by the filename actually appearing before moving
                # on, retrying the selection itself if it doesn't show up yet.
                lines_out += [
                    "  for (let attempt = 0; attempt < 4; attempt++) {",
                    f"    await {fill_input}.setInputFiles({_js_string(p)});",
                    "    await new Promise(r => setTimeout(r, 1500));",
                    f"    if (await ctx.getByText({fname_js}, {{ exact: false }}).count() > 0) break;",
                    "  }",
                ]
            return lines_out
        if a == "select_all":
            return [f"  await checkSelectAll(ctx, {target});"]
        if a == "open_row_menu":
            return [f"  await openRowMenu(ctx, {target});"]
        if a == "check_named":
            return [
                f"  await (await pickScoped(dlg(ctx).getByRole('checkbox', {{ name: {target} }}), "
                f"ctx.getByRole('checkbox', {{ name: {target} }}))).check();",
            ]
        if a == "select_dropdown_option":
            return [f"  await selectDropdownOption(ctx, {target});"]
        if a == "download":
            counters["dl"] = counters.get("dl", 0) + 1
            n = counters["dl"]
            return [
                f"  const [download{n}] = await Promise.all([",
                "    page.waitForEvent('download'),",
                f"    clickByText(ctx, {target}),",
                "  ]);",
                f"  const savedPath{n} = path.join(DOWNLOADS_DIR, download{n}.suggestedFilename());",
                f"  await download{n}.saveAs(savedPath{n});",
                f"  downloadedFiles.push(savedPath{n});",
                f"  expect(fs.existsSync(savedPath{n})).toBeTruthy();",
                f"  expect(fs.statSync(savedPath{n}).size).toBeGreaterThan(0);",
            ]
        if a == "expect_zip_contains":
            names = [n.strip() for n in st.get("value", "").split(",") if n.strip()]
            if not names:
                return []
            checks = "\n".join(
                f"      expect(zipNames.some(n => n.endsWith({_js_string(nm)}))).toBeTruthy();"
                for nm in names)
            return [
                "  {",
                "    const lastFile = downloadedFiles[downloadedFiles.length - 1];",
                "    if (lastFile && lastFile.toLowerCase().endsWith('.zip')) {",
                "      const zipNames = new AdmZip(lastFile).getEntries().map(e => e.entryName);",
                checks,
                "    }",
                "  }",
            ]
        return []

    body = ""
    for i, sc in enumerate(scenarios, 1):
        title = sc["title"].replace("'", "\\'")
        # Static per-test annotation (Playwright's `test(title, { annotation }, fn)`
        # form) — deterministic, host-supplied ticket key (not LLM-authored).
        # allure-playwright resolves it into a clickable Jira link on the test
        # via the `links.issue` urlTemplate in playwright.config.ts.
        ticket_annotation = ticket_key.replace("'", "\\'")
        annotation = f"{{ annotation: {{ type: 'issue', description: '{ticket_annotation}' }} }}"
        body += f"\n// [{i}] {sc['comment']}\n"
        body += f"test('{title}', {annotation}, async ({{ page }}) => {{\n"
        # The default 30s test timeout is tuned for short/simple flows — a
        # multi-step journey (create -> upload -> download -> verify) needs
        # much longer once real network/app latency and settle waits add up.
        # view_file_preview steps break the flat 8s/step assumption: each one
        # can alone wait up to the viewer's own 240s render timeout (see that
        # action's render logic above), so every occurrence adds its own
        # budget on top rather than being absorbed by the generic per-step rate.
        view_preview_count = sum(1 for st in sc["steps"] if st["action"] == "view_file_preview")
        step_timeout_ms = max(60_000, min(1_500_000, len(sc["steps"]) * 8_000 + view_preview_count * 360_000))
        body += f"  test.setTimeout({step_timeout_ms});\n"
        body += "  const ctx = await openPage(page);\n"
        scenario_needs_download = any(st["action"] in ("download", "expect_zip_contains")
                                      for st in sc["steps"])
        if scenario_needs_download:
            body += "  const downloadedFiles: string[] = [];\n"
        scenario_needs_now = any(_NOW_PLACEHOLDER in (st.get("target", "") + st.get("value", ""))
                                 for st in sc["steps"])
        if scenario_needs_now:
            body += "  const NOW = nowToken();\n"
        # Declared once here (not `const` at the point of the capture_files
        # step) so a LATER, separate test.step() callback can still see it --
        # each test.step() body is its own closure. `let`, since capture_files
        # assigns into it. Same variable name for every capture_files/
        # expect_same_files step sharing the same `target` text (_js_ident is
        # a pure function of that text), so an expect_same_files step
        # anywhere later in the scenario reads the right one.
        capture_vars = sorted({_js_ident(st.get("target", "files")) for st in sc["steps"]
                               if st["action"] in ("capture_files", "expect_same_files")})
        for var in capture_vars:
            body += f"  let {var}: string[] = [];\n"
        # Wired up before the first step, not just around the historically
        # flaky ones (view_file_preview) — the upload-verification failure
        # seen live (folder still empty after two "successful" uploads, no
        # console/network evidence at all in the report) showed that ANY step
        # can be the one that needs this, not just the ones anticipated in
        # advance. Wrapping the whole step sequence in one try/catch means
        # every future failure — whichever step it turns out to be — attaches
        # this automatically instead of only the steps someone thought to
        # instrument.
        body += "  const pageDebugLog = attachPageDebugLog(page);\n"
        body += "  try {\n"
        counters: dict = {}
        # Consecutive steps sharing the same "section" (e.g. "Create
        # Workspace", "Archive Files") are wrapped in one test.step() —
        # collapses a long flat step list into labeled, collapsible groups in
        # the HTML report. A step with no section runs inline, unwrapped, so
        # this is fully backward-compatible with scenarios that don't set it.
        current_section = None
        section_open = False
        for st in sc["steps"]:
            section = st.get("section")
            if section != current_section:
                if section_open:
                    body += "  });\n"
                    section_open = False
                if section:
                    body += f"  await test.step({_js_string(section)}, async () => {{\n"
                    section_open = True
                current_section = section
            for line in step_lines(st, counters):
                body += line + "\n"
        if section_open:
            body += "  });\n"
        body += "  } catch (e) {\n"
        body += ("    await test.info().attach('page-debug.log', "
                  "{ body: pageDebugLog.join('\\n') || "
                  "'(no console/pageerror/network-failure events captured)', "
                  "contentType: 'text/plain' });\n")
        body += "    throw e;\n"
        body += "  }\n"
        body += "});\n"

    return header + body


def gates(spec_path: Path, expected_tests: int) -> list[str]:
    """Deterministic checks. Returns list of failures (empty = all pass)."""
    fails = []
    src = spec_path.read_text(encoding="utf-8")
    # balance checks ignore // comment text (avoids false positives like "1)")
    code_only = "\n".join(line.split("//")[0] for line in src.splitlines())
    n = src.count("test('")
    if n != expected_tests:
        fails.append(f"count gate: expected {expected_tests} tests, file has {n}")
    if code_only.count("{") != code_only.count("}"):
        fails.append("brace balance gate failed")
    if code_only.count("(") != code_only.count(")"):
        fails.append("paren balance gate failed")
    for req in ("@playwright/test", "openPage", "SEL_EMAIL",
                "SEL_PASSWORD", "SEL_SUBMIT"):
        if req not in src:
            fails.append(f"content gate: missing {req}")
    annotation_count = src.count("annotation: { type: 'issue'")
    if annotation_count != expected_tests:
        fails.append(f"annotation gate: expected {expected_tests} issue "
                     f"annotations, file has {annotation_count}")
    # Selector gate: a login step that fills/clicks SEL_PASSWORD/SEL_SUBMIT when
    # that selector was never discovered (progressive login -- the field only
    # appears after an earlier step, e.g. entering the email) renders as
    # ctx.locator('').fill(...), which Playwright rejects at runtime with a
    # cryptic "Unexpected token \"\" while parsing css selector" error --
    # several browsers hit exactly this in practice. Caught here
    # instead: a clear, pre-flight failure before Playwright ever launches.
    if "const SEL_EMAIL    = '';" in src:
        fails.append("selector gate: no email selector was discovered for this "
                     "environment's login page")
    if "const SEL_PASSWORD = '';" in src and "SEL_PASSWORD)." in src:
        fails.append("selector gate: a step fills/clicks SEL_PASSWORD, but no "
                     "password selector was discovered (progressive login) -- "
                     "use fill_named/click_text for the password step instead "
                     "of the fixed field-based login action")
    if "const SEL_SUBMIT   = '';" in src and "SEL_SUBMIT)." in src:
        fails.append("selector gate: a step fills/clicks SEL_SUBMIT, but no "
                     "submit selector was discovered (progressive login) -- "
                     "use click_text for the submit step instead of the fixed "
                     "field-based login action")
    # Credential gate: a real secret must never be baked into a generated spec —
    # only the two runtime env-var references are allowed to embed VALID_USER/
    # VALID_PASS. Anything else on those two lines means a literal leaked in.
    for line in src.splitlines():
        if line.startswith("const VALID_USER") and line.strip() != "const VALID_USER = process.env.TEST_USER ?? '';":
            fails.append("credential gate: VALID_USER is not reading from process.env")
        if line.startswith("const VALID_PASS") and line.strip() != "const VALID_PASS = process.env.TEST_PASS ?? '';":
            fails.append("credential gate: VALID_PASS is not reading from process.env")
    return fails
