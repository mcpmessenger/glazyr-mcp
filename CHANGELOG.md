# Changelog

## 0.3.4 - unreleased (branch 0.3.4)

Glazyr Trace, per TRACE_FORMAT.md (Draft v0.4, copied into this repo
with its conformance fixtures). Bridge and launcher only; the browser
bundle (0.3.0) is unchanged and no producer changes were made.

- Trace writer: every client attach is a trace session. Reads
  (`state_read`, with `scope` for summary / find / subtree), write-ahead
  `action` events and their `action_outcome` (with provenance under the
  section 5.4 honesty rules), `invalidation` of bridge-asserted state,
  and `freshness` transitions of the producer are appended to
  `~/.glazyr/traces/glazyr-<pid>-<utc>.trace.jsonl`: JSON Lines, RFC 8785
  (JCS) + SHA-256 hash chain from the genesis seed, one writer lock
  shared by the tool path and the resident heartbeat. A restarted
  bridge starts a new file and links back with `session_start.continues`.
  Bridge-initiated browser relaunches are recorded as
  `cause: bridge_recovery` actions.
- New tool `glazyr_trace` (section 13): modes summary / tail / failures /
  url / since / verify / note, `file=` to read a predecessor's trace,
  a footer with the response's own token cost, the chain head hash and
  chain status, and the section 13.4 cut order under budget.
- `VERSION` constant in the bridge; the tokenizer branch taken at import
  (`cl100k` or `chars/4`) is recorded and named on every token figure.
- `_click_with_fallback` reports which method effected the click
  (`mouse` / `dom_click` / `form_submit`).
- Fix: on producer death the guard now clears the bridge overlay too
  (0.3.3 kept it, so a dead browser's typed values / CHECKED marks
  could be applied by DOM id to the fresh browser's page), bumps
  `overlay_version`, and emits `invalidation{producer_dead}`.
- Fix: a dead browser is no longer served as live. Every read called
  `ensure_browser()` before the guard; it silently replaced a dead
  browser, so the guard saw the newcomer alive, and when the new
  producer's seq equalled the old one `refresh_apc` (keyed on seq only)
  kept serving the dead page and its overlay - and actions resolved
  stale node ids against it. Producer identity (pid + start time) is now
  part of state identity: a state written by a producer other than the
  one writing now is dropped as `producer_dead` (overlay cleared,
  invalidation emitted) and the next call answers GLAZYR_PRODUCER_DEAD,
  without a second relaunch; a navigate that discovers it says so.
  `ensure_browser`'s relaunch is traced as a `bridge_recovery` action.
  Drill: `tests/test_dead_browser.py`, `tests/acceptance/drill_dead_browser.py`.
- Fix: values typed into password (credential) fields are never echoed:
  not in the type ack, not in later peeks / finds (overlay and the
  producer's own field value), not as a find match, not in the trace
  (`value_redacted: true`), and credential-named query parameters of a
  GET-submitted form show as `REDACTED` in URLs.
- Launcher: adds `rfc8785` to the bridge venv (and to venvs created by
  earlier versions). Without it the bridge runs with tracing off and
  `glazyr_trace` says why. `GLAZYR_TRACE=off` disables tracing;
  `GLAZYR_TRACE_DIR` moves it.

Compatibility: all 0.3.3 tools and outputs unchanged apart from the two
fixes; diagnostic suite 19/19 PASS (same as 0.3.3); Gauntlet smoke
outputs byte-identical to the 0.3.3 post-publish smoke.

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
