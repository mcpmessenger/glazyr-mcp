# Changelog

## 0.3.3 - 2026-11-24

Resident-mode survival fixes, found by the v0.3.2 registry smoke
test. Bridge only; the launcher and the browser bundle (0.3.0) are
unchanged.

- The resident bridge now daemonizes at startup (fork + setsid).
  Some clients (mcporter 0.17.x) retire their whole process tree
  on transport close; the resident, spawned by the launcher, was
  a descendant of the client's server process and received
  SIGTERM with the tree - page state was lost between calls.
  Orphaned to init, the resident is in no client's tree and
  survives.
- A protocol shield classifies raw socket lines before the MCP
  SDK validator: unknown methods (e.g. the new-protocol
  `server/discover`) get a well-formed JSON-RPC -32601, unknown
  notifications are dropped, malformed lines get -32700 / -32600,
  and a session that ends in an exception closes only its own
  connection - the resident keeps serving.

Compatibility: no tool or protocol changes for existing clients;
verified against the newest-protocol client path (two-call state
persistence) and by an OpenClaw end-to-end regression run (8/8).

## 0.3.2 - 2026-11-24

Baseline import of the shipped 0.3.2 sources into version control
(0.3.2 was originally released from a staging tree). Resident
bridge session, producer liveness / state-freshness checks,
suite-runner fix.
