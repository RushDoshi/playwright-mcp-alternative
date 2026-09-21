#!/usr/bin/env node
/* Cross-platform Allure report generation. Never fails the build: if the
 * Allure CLI or results are missing, it logs and exits 0. This replaces a
 * shell one-liner that used ${TICKET_KEY} syntax (broken on Windows cmd) and
 * `||` chaining (also shell-specific). */
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const ticket = process.env.TICKET_KEY || 'local';
const base = path.join('results', ticket);
const resultsDir = path.join(base, 'allure-results');
const reportDir = path.join(base, 'allure-report');

if (!fs.existsSync(resultsDir)) {
  console.log(`[report] no allure-results at ${resultsDir} — nothing to generate (this is fine).`);
  process.exit(0);
}
try {
  execFileSync('allure', ['generate', resultsDir, '--clean', '-o', reportDir], { stdio: 'inherit' });
  console.log(`[report] Allure report written to ${reportDir}`);
} catch (e) {
  console.log('[report] Allure CLI not installed or failed — skipping (optional). ' +
              'Install it from https://allurereport.org if you want Allure output.');
  process.exit(0);
}
