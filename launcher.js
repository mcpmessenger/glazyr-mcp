#!/usr/bin/env node
// Glazyr MCP launcher.
//
// Zero-dependency Node shim whose only job is to make
//   npx @glazyr/mcp
// behave like a native MCP server:
//   1. ensure a Python venv with the bridge's dependencies exists
//   2. ensure the patched Chromium (headless_shell) bundle is downloaded
//   3. connect the client to the bridge over stdio and get out of the way
//
// v0.3.2: step 3 now goes through the RESIDENT bridge. The first
// invocation starts a single-instance resident bridge (detached, on a
// Unix socket under ~/.glazyr); every invocation - persistent clients
// and per-call shells like mcporter alike - attaches to it, so the
// browser, page and sequence state survive between calls. A dead or
// wedged resident is detected (pid + heartbeat + socket probe),
// reclaimed, and restarted transparently; state loss after such a
// restart is visible (fresh browser, seq reset), never papered over.
// Set GLAZYR_RESIDENT=off to spawn a private bridge exactly as
// v0.3.1 did. If the resident cannot be started, the launcher falls
// back to that private spawn - a resident bug must never brick a
// client.
//
// Everything is installed under ~/.glazyr (override with GLAZYR_HOME).
// All diagnostics go to stderr; stdout belongs to the MCP protocol.

const fs = require('fs');
const os = require('os');
const path = require('path');
const net = require('net');
const https = require('https');
const http = require('http');
const { spawn, spawnSync } = require('child_process');

const VERSION = require('./package.json').version;
const HOME = process.env.GLAZYR_HOME || path.join(os.homedir(), '.glazyr');
const VENV = path.join(HOME, 'venv');
const VENV_PY = path.join(VENV, 'bin', 'python');
const BROWSER_DIR = path.join(HOME, 'browser');
const SHELL = path.join(BROWSER_DIR, 'headless_shell');
const BRIDGE = path.join(__dirname, 'bridge', 'glazyr_mcp.py');
// The browser bundle versions independently of this package: a
// bridge-only release must keep pointing at the GitHub release that
// actually carries the bundle, or every fresh install 404s on first
// run. Bump BUNDLE_VERSION only when the browser binary changes.
const BUNDLE_VERSION = '0.3.0';
const BUNDLE_URL = process.env.GLAZYR_BUNDLE_URL ||
  `https://github.com/mcpmessenger/glazyr-mcp/releases/download/v${BUNDLE_VERSION}/glazyr-browser-linux-x64.tar.xz`;
const PIP_PACKAGES = ['mcp<2', 'protobuf', 'websocket-client', 'tiktoken', 'rfc8785'];

// Resident bridge rendezvous (all under HOME).
const RES_SOCK = path.join(HOME, 'resident.sock');
const RES_JSON = path.join(HOME, 'resident.json');
const RES_HB = path.join(HOME, 'resident.heartbeat');
const RES_LOG = path.join(HOME, 'resident.log');
const RESIDENT_OFF = ['0', 'off', 'false', 'no'].includes(
  (process.env.GLAZYR_RESIDENT || '').toLowerCase());
const HB_MAX_AGE_MS = 20000;      // resident touches heartbeat every 5s
const RES_START_TIMEOUT_MS = 30000;

function log(...args) { console.error('[glazyr]', ...args); }
function die(msg) { log('error:', msg); process.exit(1); }

function run(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, { stdio: ['ignore', 'pipe', 'inherit'], ...opts });
  if (r.error) die(`failed to run ${cmd}: ${r.error.message}`);
  return r;
}

function checkPlatform() {
  if (process.platform === 'win32') {
    die('on Windows, run Glazyr inside WSL2 — add it to your MCP config via wsl.exe ' +
        '(see the README), not via npx directly.');
  }
  if (process.platform !== 'linux' || process.arch !== 'x64') {
    die(`no prebuilt browser bundle yet for ${process.platform}/${process.arch} ` +
        '(Linux x64 and Windows-via-WSL are supported today).');
  }
}

// v0.3.4: Glazyr Trace canonicalizes with rfc8785 (RFC 8785 JCS). A
// venv created by an earlier version lacks it: add it once. If that
// fails the bridge still runs, with tracing off - glazyr_trace says so.
function ensureTraceDep() {
  const probe = spawnSync(VENV_PY, ['-c', 'import rfc8785'], { stdio: 'ignore' });
  if (probe.status === 0) return;
  log('adding rfc8785 (Glazyr Trace) to', VENV);
  const r = spawnSync(VENV_PY, ['-m', 'pip', 'install', '--quiet', 'rfc8785'],
    { stdio: ['ignore', 'ignore', 'inherit'] });
  if (r.status !== 0) log('warning: rfc8785 install failed; tracing will be off.');
}

function ensureVenv() {
  if (fs.existsSync(VENV_PY)) { ensureTraceDep(); return; }
  log('first run: creating Python environment in', VENV);
  const probe = run('python3', ['--version']);
  if (probe.status !== 0) die('python3 not found — install Python 3.10+ and retry.');
  const ver = (probe.stdout || '').toString().trim();
  const m = ver.match(/Python 3\.(\d+)/);
  if (!m || parseInt(m[1], 10) < 10) die(`need Python 3.10+, found: ${ver || 'unknown'}`);
  fs.mkdirSync(HOME, { recursive: true });
  let r = run('python3', ['-m', 'venv', VENV]);
  if (r.status !== 0) die('python3 -m venv failed (on Debian/Ubuntu: apt install python3-venv).');
  r = run(VENV_PY, ['-m', 'pip', 'install', '--quiet', ...PIP_PACKAGES]);
  if (r.status !== 0) die('pip install of bridge dependencies failed — see output above.');
  log('Python environment ready.');
}

function download(url, dest, redirects = 0) {
  return new Promise((resolve, reject) => {
    if (redirects > 5) return reject(new Error('too many redirects'));
    const transport = url.startsWith('https:') ? https : http;
    transport.get(url, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        res.resume();
        return resolve(download(res.headers.location, dest, redirects + 1));
      }
      if (res.statusCode !== 200) {
        res.resume();
        return reject(new Error(`download failed: HTTP ${res.statusCode} for ${url}`));
      }
      const total = parseInt(res.headers['content-length'] || '0', 10);
      let seen = 0, lastPct = -1;
      const out = fs.createWriteStream(dest);
      res.on('data', (chunk) => {
        seen += chunk.length;
        if (total) {
          const pct = Math.floor((seen / total) * 100);
          if (pct >= lastPct + 10) { lastPct = pct; log(`browser bundle: ${pct}%`); }
        }
      });
      res.pipe(out);
      out.on('finish', () => out.close(resolve));
      out.on('error', reject);
    }).on('error', reject);
  });
}

async function ensureBrowser() {
  if (fs.existsSync(SHELL)) return;
  log('first run: downloading the Glazyr browser bundle (~100 MB) from', BUNDLE_URL);
  fs.mkdirSync(BROWSER_DIR, { recursive: true });
  const tmp = path.join(HOME, 'bundle.tar.xz');
  await download(BUNDLE_URL, tmp).catch((e) => die(e.message));
  const r = run('tar', ['-xJf', tmp, '-C', BROWSER_DIR, '--strip-components=1']);
  fs.unlinkSync(tmp);
  if (r.status !== 0 || !fs.existsSync(SHELL)) {
    die('browser bundle extraction failed (needs tar with xz support).');
  }
  fs.chmodSync(SHELL, 0o755);
  log('browser bundle ready.');
}

// ---------- resident bridge ----------

function readResident() {
  try {
    return JSON.parse(fs.readFileSync(RES_JSON, 'utf8'));
  } catch {
    return null;
  }
}

function pidAlive(pid) {
  if (!pid) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

// Is `pid` really a Glazyr bridge? Guards against pid reuse after a
// crash - we only ever reclaim/kill a process we can identify.
function pidIsBridge(pid) {
  try {
    const cmd = fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8');
    return cmd.includes('glazyr_mcp');
  } catch {
    return false;
  }
}

function heartbeatFresh() {
  try {
    const st = fs.statSync(RES_HB);
    return (Date.now() - st.mtimeMs) < HB_MAX_AGE_MS;
  } catch {
    return false;
  }
}

function probeSocket(timeoutMs = 1500) {
  return new Promise((resolve) => {
    const s = net.connect(RES_SOCK);
    const done = (ok) => { try { s.destroy(); } catch {} resolve(ok); };
    s.setTimeout(timeoutMs);
    s.once('connect', () => done(true));
    s.once('timeout', () => done(false));
    s.once('error', () => done(false));
  });
}

// Healthy = identifiable bridge process, alive, heartbeating, and its
// socket accepts a connection. Anything else is reclaimed, never
// attached to.
async function residentHealthy() {
  const info = readResident();
  if (!info || !info.pid) return false;
  if (!pidAlive(info.pid) || !pidIsBridge(info.pid)) return false;
  if (!heartbeatFresh()) return false;
  if (info.sock && info.sock !== RES_SOCK) return false;
  return probeSocket();
}

async function reclaimResident() {
  const info = readResident();
  if (info && info.pid && pidAlive(info.pid) && pidIsBridge(info.pid)) {
    log(`reclaiming stale resident bridge (pid ${info.pid})`);
    try { process.kill(info.pid, 'SIGTERM'); } catch {}
    const t0 = Date.now();
    while (pidAlive(info.pid) && Date.now() - t0 < 3000) {
      await new Promise((r) => setTimeout(r, 100));
    }
    if (pidAlive(info.pid)) {
      try { process.kill(info.pid, 'SIGKILL'); } catch {}
    }
  }
  for (const f of [RES_SOCK, RES_JSON, RES_HB]) {
    try { fs.unlinkSync(f); } catch {}
  }
}

async function startResident() {
  fs.mkdirSync(HOME, { recursive: true });
  const logFd = fs.openSync(RES_LOG, 'a');
  const child = spawn(VENV_PY, [BRIDGE, '--resident'], {
    detached: true,
    stdio: ['ignore', logFd, logFd],
    env: { ...process.env, GLAZYR_SHELL: SHELL },
  });
  child.on('error', () => {});
  child.unref();
  fs.closeSync(logFd);
  const t0 = Date.now();
  while (Date.now() - t0 < RES_START_TIMEOUT_MS) {
    if (await residentHealthy()) return true;
    await new Promise((r) => setTimeout(r, 250));
  }
  return false;
}

// Relay this process's stdio to the resident's socket. The MCP stdio
// framing (newline-delimited JSON) is identical on the socket, so the
// relay is a raw byte pipe. When the client goes away we hang up; the
// resident and its browser stay up for the next invocation.
function attachRelay() {
  return new Promise((resolve) => {
    const sock = net.connect(RES_SOCK);
    let settled = false;
    const done = (code) => {
      if (settled) return;
      settled = true;
      try { sock.destroy(); } catch {}
      resolve(code);
    };
    sock.once('error', (e) => {
      log('resident connection failed:', e.message);
      done(1);
    });
    sock.once('connect', () => {
      process.stdin.pipe(sock);
      sock.pipe(process.stdout);
    });
    sock.once('close', () => done(0));
    process.stdin.once('end', () => { try { sock.end(); } catch {} });
    for (const sig of ['SIGINT', 'SIGTERM']) {
      process.on(sig, () => done(0));
    }
  });
}

async function runResident() {
  if (await residentHealthy()) {
    const code = await attachRelay();
    if (code === 0) return true;
    // Resident died or refused mid-attach: fall through and restart
    // it once rather than failing the client outright.
    log('resident attach failed; restarting resident');
  }
  await reclaimResident();
  if (!(await startResident())) return false;
  const code = await attachRelay();
  return code === 0;
}

// v0.3.1 behaviour: a private bridge bound to this process's stdio.
function runPrivate() {
  return new Promise((resolve) => {
    const child = spawn(VENV_PY, [BRIDGE], {
      stdio: 'inherit',
      env: { ...process.env, GLAZYR_SHELL: SHELL },
    });
    for (const sig of ['SIGINT', 'SIGTERM']) {
      process.on(sig, () => child.kill(sig));
    }
    child.on('exit', (code, signal) => {
      if (signal) process.kill(process.pid, signal);
      resolve(code == null ? 1 : code);
    });
    child.on('error', (e) => { die(`failed to start bridge: ${e.message}`); resolve(1); });
  });
}

async function main() {
  checkPlatform();
  ensureVenv();
  await ensureBrowser();
  if (!RESIDENT_OFF) {
    try {
      if (await runResident()) process.exit(0);
      log('resident bridge unavailable; falling back to a private bridge');
    } catch (e) {
      log('resident bridge error:', e.message, '; falling back to a private bridge');
    }
  }
  process.exit(await runPrivate());
}

main().catch((e) => die(e.message));
