#!/usr/bin/env node
/* Standalone static file server for the demo login page. Runs as its OWN
 * process (see demo-app/run-demo.js) so its event loop is never blocked by the
 * Playwright run -- an in-process server sharing the event loop with a
 * blocking test spawn cannot answer requests, which resets the browser's
 * connection mid-navigation. Binds 0.0.0.0 so both 127.0.0.1 and ::1 reach it.
 */
const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = Number(process.env.DEMO_PORT || 4173);
const ROOT = __dirname;

const server = http.createServer((req, res) => {
  const rel = (req.url === '/' ? '/login.html' : req.url).split('?')[0];
  const file = path.join(ROOT, path.normalize(rel));
  if (!file.startsWith(ROOT) || !fs.existsSync(file)) {
    res.writeHead(404); res.end('not found'); return;
  }
  res.writeHead(200, { 'Content-Type': 'text/html' });
  fs.createReadStream(file).pipe(res);
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`[demo] static server on http://127.0.0.1:${PORT} (pid ${process.pid})`);
});

// Exit cleanly when the orchestrator asks.
process.on('SIGTERM', () => server.close(() => process.exit(0)));
process.on('SIGINT', () => server.close(() => process.exit(0)));
