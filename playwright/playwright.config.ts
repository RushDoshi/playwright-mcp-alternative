import { defineConfig } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

// Per-ticket results dir so one ticket's report never overwrites another's.
// TICKET_KEY is set by the host when it runs a generated spec; defaults to
// 'local' for a manual `npx playwright test`.
const TICKET = process.env.TICKET_KEY || 'local';
const RESULTS_BASE = `results/${TICKET}`;

// Core reporters that need no optional dependency: list (stdout tail for the
// Jira comment), junit (parsed by the host's junit_parser), and Playwright's
// own HTML report.
const reporters: any[] = [
  ['list'],
  ['junit', { outputFile: `${RESULTS_BASE}/junit.xml` }],
  ['html', { outputFolder: `${RESULTS_BASE}/html-report`, open: 'never' }],
];

// Add the Allure reporter ONLY if allure-playwright is actually installed.
// Detected by looking for its package folder in node_modules rather than with
// require.resolve() — `require` is not reliably defined under Playwright's
// TS/ESM config loader, and letting a ReferenceError escape here would take
// down the entire config. fs.existsSync() is always safe.
if (fs.existsSync(path.join(__dirname, 'node_modules', 'allure-playwright'))) {
  reporters.push(['allure-playwright', { resultsDir: `${RESULTS_BASE}/allure-results` }]);
}

export default defineConfig({
  testDir: './suites',
  timeout: 120_000,
  fullyParallel: false,
  reporter: reporters,
  use: {
    baseURL: process.env.BASE_URL,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    headless: process.env.PLAYWRIGHT_HEADED ? false : true,
  },
  // The host runs specs with --project=P0/P1/P2 (priority tags). Three projects
  // that differ only by the @p0/@p1/@p2 grep so any subset can be selected.
  projects: [
    { name: 'P0', grep: /@p0/ },
    { name: 'P1', grep: /@p1/ },
    { name: 'P2', grep: /@p2/ },
  ],
});
