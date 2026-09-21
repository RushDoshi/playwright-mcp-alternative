#!/usr/bin/env node
/* Orchestrates the demo as THREE separate processes so nothing blocks the
 * server's event loop:
 *   1. spawn demo-app/serve.js (background, async spawn -- not spawnSync)
 *   2. poll until it answers an HTTP GET
 *   3. spawn `playwright test` against it, inheriting stdio
 *   4. kill the server, exit with the test's code
 */
const http = require('http');
const path = require('path');
const { spawn } = require('child_process');

const PORT = Number(process.env.DEMO_PORT || 4173);
const HERE = __dirname;

const server = spawn(process.execPath, [path.join(HERE, 'serve.js')],
                     { stdio: 'inherit', env: { ...process.env, DEMO_PORT: String(PORT) } });

let done = false;
function stopServer() {
  if (!server.killed) { try { server.kill('SIGTERM'); } catch {} }
}
process.on('exit', stopServer);
process.on('SIGINT', () => { stopServer(); process.exit(1); });

function waitReady(attempt = 0) {
  http.get(`http://127.0.0.1:${PORT}/login.html`, (r) => {
    r.resume();
    if (r.statusCode === 200) return runTests();
    retry(attempt);
  }).on('error', () => retry(attempt));
}
function retry(attempt) {
  if (attempt > 50) { console.error('[demo] server never became ready'); stopServer(); process.exit(1); }
  setTimeout(() => waitReady(attempt + 1), 100);
}

function checkInstalled() {
  const fs = require('fs');
  const marker = path.join(HERE, '..', 'node_modules', '@playwright', 'test');
  if (!fs.existsSync(marker)) {
    console.error(
      '\n[demo] @playwright/test is not installed in this folder.\n' +
      `[demo] Run this in ${path.join(HERE, '..')} first:\n` +
      '[demo]   npm install\n' +
      '[demo]   npx playwright install\n' +
      '[demo] Then re-run: npm run demo\n'
    );
    stopServer();
    process.exit(1);
  }
}

function runTests() {
  checkInstalled();
  const env = {
    ...process.env,
    BASE_URL: `http://127.0.0.1:${PORT}`,
    TEST_USER: 'demo@example.com',
    TEST_PASS: 'Passw0rd!',
    TICKET_KEY: 'DEMO',
  };
  const npx = process.platform === 'win32' ? 'npx.cmd' : 'npx';
  const t = spawn(npx, ['playwright', 'test', 'p0/DEMO.spec.ts', '--project=P0', '--project=P1'],
                  { stdio: 'inherit', env });
  t.on('close', (code) => { done = true; stopServer(); process.exit(code ?? 1); });
}

waitReady();
