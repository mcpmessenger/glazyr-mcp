# TRACE_FORMAT — Glazyr Trace event format

**Status:** Draft v0.4 — pass 1 (event schema), pass 2 (the
`glazyr_trace` tool contract, §13), and the cold stranger test are
complete: a cold implementer reproduced all 8 fixture verdicts and
emitted a valid trace from this document alone, and its 25-point
ambiguity list has been adjudicated into this revision (Nov 26, 2026).
Conformance fixtures live in `trace-fixtures/` beside this file.
**Date:** 2026-11-26.
**Design provenance:** four Claude Code consult rounds (Nov 26, 2026),
conducted from the consumer's seat and verified line-by-line against the
shipped Glazyr 0.3.3 bridge. Scope and amendments: `glazyr-0.3.4-backlog.md`
item 6 in this folder.

---

## 1. What a trace is

A trace is a local, append-only record of a browsing session: what state
was read, what actions were taken, what they claimed to do, and what the
emitter actually knew when it claimed it.

A trace exists, in order, to:

1. **Resume.** A fresh agent instance, inheriting only the trace, picks
   up a dead session's task without re-reading full pages.
2. **Explain.** A human or a judge can reconstruct what the agent did,
   in what order, at what token cost.
3. **Audit.** The hash chain lets a reader detect torn, lost, or spliced
   lines after the fact.

A trace is **not** a telemetry stream, not a hosted log, and not a
signature. Nothing in this format leaves the machine unless its owner
moves it.

## 2. Principles

- **Resume first, log second.** Every design choice is tested against
  the cold-resume case before the pretty-log case.
- **Provenance is mandatory.** Every outcome carries how the emitter
  knows it: `observed`, `asserted`, or `inferred` (§7). A trace that
  cannot distinguish "the page confirmed it" from "the bridge claims it"
  is a liability dressed as a record.
- **Absent is never null.** A field an emitter cannot honestly fill is
  *omitted* — never written as `null`, `0`, or `""` (§9). Absence means
  "this stack can't know," and readers must treat it that way.
- **Qualified numbers.** A hash is meaningless without its basis; a token
  count is meaningless without its tokenizer. Both travel with their
  qualifier or not at all.
- **Accident detection, not tamper-evidence.** The hash chain (§6)
  catches torn, lost, and spliced lines. Anyone who can edit the file
  can recompute the chain, so this format never claims otherwise.

## 3. File format

- **JSON Lines**: one event per line, UTF-8, LF-terminated. Readers
  split on LF and strip at most one trailing CR per line. A blank line
  is unparseable (a chain break, §6), and so is a UTF-8 BOM. Nothing in
  this format is ever pretty-printed across lines.
- **Append-only.** A writer never modifies or deletes an earlier line.
  Each append is a single serialized write (writers that can be called
  concurrently must hold a lock across "read head hash, append line").
- **One writer per file.** A file belongs to one emitter instance. Where
  one emitter serves many clients (a resident bridge), sessions
  interleave in the one file and are disambiguated by the `session`
  field on every event (§4). The hash chain is per file, not per session.
- **A writer never appends to a file it did not create.** A restarted
  emitter starts a new file and links back with `continues` (§5.1).
- **Naming is the writer's choice.** Recommended:
  `<instance>-<yyyymmdd-hhmmss>.trace.jsonl`.
- **Readers stream.** A trace grows with its session; readers process
  it line by line and never assume it fits in memory.

### Data constraints (normative)

These constraints exist so that two implementations in different
languages produce byte-identical canonical forms (§6):

- **Integers only.** Every number in a trace is an integer in the range
  0 ≤ x ≤ 2^53−1. No floats, no negative zero, no exponent forms.
  Durations are integer milliseconds. State sequence numbers are capped
  by this range; the `epoch` (§5.2) absorbs resets before the cap is a
  practical concern.
- **No `NaN`, no `Infinity`.** They are not JSON. (Python emitters:
  serialize with `allow_nan=False`.)
- **Strings are valid Unicode scalar values.** Lone surrogates are
  replaced with U+FFFD *before* writing — page text can contain them,
  and implementations disagree on their encoding, which would fork the
  hash chain. String truncation (e.g. signature text) is **by code
  point**, never by UTF-16 unit. A reader that encounters a
  lone-surrogate escape in a line treats that line as unparseable for
  chain purposes (a break, §6) and reports a conformance violation:
  its hash is not well-defined across implementations.
- **Reader checks vs writer honesty.** Conformance rules split in two.
  Readers mechanically enforce: no `null` values, no duplicate keys,
  integer-only numbers, valid-scalar strings. A line violating one of
  these is non-conformant (and, where hashing is affected, a chain
  break). The ban on placeholder `""` and `0` is a **writer-honesty**
  rule: both are often honest values (an input with no text, a zero
  count), so no reader can detect a placeholder mechanically, and none
  should try. Floats and out-of-range integers are valid JSON and
  canonicalize fine (JCS maps `1.0` and `1` to the same bytes — so the
  chain cannot detect that rewrite, a known limit): readers report
  them as conformance violations on that line; the chain is unaffected.
- **No Unicode normalization.** Emitters must not normalize strings;
  the canonicalization scheme (§7) does not either.
- **Duplicate keys are forbidden** in any object. Parsers resolve them
  silently and differently.

## 4. Common fields

Every event carries:

| Field | Type | Meaning |
|---|---|---|
| `v` | integer | Format version of this event. Currently `1`. |
| `n` | integer ≥ 1 | The writer's monotonic event index: +1 per line, no gaps in a healthy file. **This is the step id.** A gap means a lost line. `n` is **per file**: it starts at 1 in every file — including a continuation file (§5.1) — and in a healthy file it equals the 1-based line number. A mismatch between `n` and line number is a conformance violation. References to events (e.g. `ref`, `dropped`) use `n`, are **file-global** — in a multi-session file they may point at another session's events — and never cross files; a cross-file reference names its file (as `continues` does). |
| `ts` | string | RFC 3339 UTC timestamp with milliseconds, e.g. `2026-11-27T18:04:31.207Z`. Writers produce exactly this form (three fractional digits, `Z`). Readers flag deviations as conformance violations but still process the event. |
| `kind` | string | Event kind (§5). |
| `session` | string | Emitter-scoped session id. For a resident bridge: one id per client attach. For a single-run agent: the run id. |
| `prev_hash` | string | SHA-256 (lowercase hex) of the previous line in the file (§6). The first event in a file uses the SHA-256 of the empty byte string. |
| `provenance` | string | `observed` \| `asserted` \| `inferred` (§7). For events carrying several claims, the weakest applies. |

No field in a trace ever has the value `null`. A field that is present
carries a real value; a field with no honest value is absent.

**`n`, not `ts`, defines event order.** Wall clocks jump (NTP
corrections, suspend/resume); `ts` is informational and may be
non-monotonic. Any logic that orders events — resume, pending fills,
change detection — orders by `n`.

## 5. Event kinds

### 5.1 `session_start`

The first event of a session. Declares who is emitting and what they
are capable of emitting — the declaration every absence in the file is
read against.

| Field | Required | Meaning |
|---|---|---|
| `emitter` | yes | `{"name": ..., "version": ...}`, e.g. `{"name": "glazyr-bridge", "version": "0.3.4"}`. |
| `stack` | yes | Free-form browsing stack, e.g. `"glazyr"`, `"playwright-mcp"`. |
| `capabilities` | yes | `{"kinds": [...], "fields": [...]}` — the event kinds this emitter can produce, and the optional field names (dotted paths, e.g. `"state.seq"`, `"target.sig"`, `"tokens"`) it can honestly fill. `kinds` is the absence contract (§9). `fields` is **advisory documentation**, not a whitelist: an emitter using a field it didn't declare is not in violation, and readers never reject a field for being undeclared. |
| `continues` | no | `{"file": ..., "head_hash": ...}` — this session continues a trace in an earlier file (emitter restart); names that file and its head hash at the hand-off. |
| `provenance` | — | Always `asserted`: the emitter asserts its own identity and capabilities. |

A session's goal is **not** a `session_start` field; goals are `note`
events (§5.7), written by the agent when it knows them.

### 5.2 `state_read`

A state was read and served to the agent. All reads are `state_read`s,
including scoped ones (finds, subtrees, summaries) — a read that spends
tokens is accounted here, never as an action.

| Field | Required | Meaning |
|---|---|---|
| `url` | when known | The state's URL. If the URL came from a fallback channel rather than the state itself, the event's `provenance` is `inferred`. |
| `title` | no | Page title. |
| `scope` | no | `page` (default, absent) \| `summary` \| `subtree` \| `find` — what slice of the state was served. |
| `state` | no | `{"epoch": ..., "seq": ...}` — a reference to the producer state this read saw. Both members optional per stack; `epoch` is an opaque string whose only contract is: it changes if and only if the `seq` space resets. |
| `doc_key` | no | The emitter's document identity (Glazyr: `document_identifier`). A changed `doc_key` is an *observed* navigation. |
| `hashes` | no | `{"basis": ..., "state": ..., "structure": ..., "outline": ..., "weak": ...}` — hex digests of, respectively: the canonical state text (below); the sorted strong-signature set; the heading outline; the weak-signature set. `basis` (e.g. `"glazyr-apc-standard"`) is required if any digest is present. **Digests from different bases must never be compared.** Any member may be absent. |
| `counts` | no | `{"total": ..., "inputs": ..., "links": ..., "headings": ...}` — node counts of the **underlying document state**, before read filtering. Any member may be absent. |
| `level` | no | Read depth, where the stack has one (Glazyr filter levels: `none` \| `light` \| `standard` \| `aggressive`). |
| `stub` | no | `true` when the read was a collapsed unchanged-state stub rather than a full state. Absent means a full read. |
| `tokens` | no | `{"count": ..., "tokenizer": ...}` — the token cost of the served read, with the counter named (e.g. `"cl100k"`, `"chars/4"`, `"tiktoken-o200k"`). |
| `read_ms` | no | Integer milliseconds the read took. |
| `error` | no | Verbatim error text. When present, **the read failed**: no state was served, and `hashes`, `counts`, and `tokens` are absent. A failed read is a first-class event — a producer-death error served to the agent changes the session exactly as much as a page does. |

**What `hashes.state` covers:** the canonical state text *only* — with
asserted overlays (typed values, CHECKED markers) excluded, and
transport framing (URL/title headers, running token counters, footers)
excluded. A hash that changes because a counter ticked, or because the
bridge asserted something, is worse than no hash: it reports change
where the page did not change.

Change detection is **derived, not stored**: a reader compares
consecutive `state_read`s for a document. A document is identified by
`doc_key`, falling back to `url` for stacks that have none. Comparisons
are made only between reads of the **same `scope`** — a `find` slice and
a full page are different servings, and comparing their hashes reports
change where none happened. `doc_key` change = navigation;
`structure` change = meaningful change; `outline` change = section-level
change; `counts.inputs` delta = form surface changed; a change confined
to `weak` = cosmetic churn. A stub read's `hashes` are freshly computed
for the current state — the stub collapses the presentation, not the
measurement. Emitters store the signals; readers draw the conclusions.

A `state_read`'s `provenance` describes the read's own claims:
`observed` when the state was served from the producer, `inferred` for
any part that came from a fallback channel (the URL rule in the table
above is the common case).

### 5.3 `action`

An action is being performed. **Action events are written ahead**: the
emitter appends the `action` event *before* performing the action, and
the outcome arrives separately (§5.4). This is the crash case the whole
format exists for — an action whose process died mid-submit (a payment,
a form post) must exist in the record, marked by its missing outcome,
or a resuming agent may perform it twice.

| Field | Required | Meaning |
|---|---|---|
| `action` | yes | Core vocabulary: `navigate` \| `click` \| `type` \| `scroll` \| `key`. Emitters may extend; readers treat unknown actions as opaque. (Reads — including finds — are `state_read`s, §5.2.) |
| `cause` | yes | `agent` \| `replay` \| `bridge_recovery` (§8). |
| `state` | no | The `{"epoch", "seq"}` state the action was taken against. |
| `url` | for `navigate` | The destination URL. |
| `target` | for element actions | Required for `click` and `type`. `scroll` and `key` carry it when they are element-scoped and omit it when they are page-scoped. `{"desc": ..., "requested_id": ..., "id": ..., "id_scope": ..., "sig": {...}}`. `desc` (a human-readable description of the target) is required. `requested_id` is the node reference the agent asked for; `id` is the reference actually acted on, with `id_scope` naming the reference space (e.g. `"dom_node_id"`, `"pw_snapshot_ref"`). When the emitter re-matches a stale reference to a different node, `requested_id` ≠ `id`, and the outcome's provenance is capped at `inferred` (§5.4). `sig` is an open object that must carry a `"scheme"` member naming its signature scheme (Glazyr: `{"scheme": "glazyr-sig", "type": ..., "href": ..., "text": ...}`; Playwright: `{"scheme": "pw-role-name", "role": ..., "name": ...}`). Node ids are per-document and shift on reload; **signatures are the durable reference** and are never cut. |
| `value` | for `type` | The value to be typed — **unless** the field is credential-bearing, in which case `value` is omitted and `value_redacted: true` is written instead (§10). A redacted fill is replayable in position only, never in content. |
| `provenance` | — | Always `asserted`: the emitter asserts it began this action against this target. Outcome claims live on the `action_outcome` event. |

An `action` with no following `action_outcome` is **interrupted**: the
action was begun, its effect is unknown, and a resuming agent must
treat non-idempotent effects (submits, posts, payments) as possibly
already performed. Interruption is judged on the **verified prefix
only** (§6): an outcome that sits beyond a chain break does not clear
its action — over-reporting an interruption is the safe direction —
and a `session_end` never clears one either (§5.8: nothing may depend
on it).

### 5.4 `action_outcome`

The outcome of an earlier `action`.

| Field | Required | Meaning |
|---|---|---|
| `ref` | yes | The `n` of the `action` event this completes. |
| `outcome` | yes | `{"status": "ok" \| "error", ...}`. On success, where knowable: `navigated` (`true` \| `false` \| `"unknown"`), `method` (`mouse` \| `dom_click` \| `form_submit` — how the action was actually effected, when the stack has a fallback chain), `new_url`, `new_doc_key`, `new_state`. On failure: `error`, the error text **verbatim**. |
| `provenance` | yes | The provenance of the outcome's claims (below). |

Outcome honesty rules:

- A navigation confirmed by a `doc_key` change is `observed` — and a
  `new_doc_key` present in the outcome is that evidence, including for
  a session's first navigation, where there is no prior key to change
  from. One concluded from a host match or a URL comparison is
  `inferred` (a URL comparison *is* evidence; it just isn't document
  identity). **With no evidence at all — neither a `doc_key` nor a URL
  comparison — `navigated` must be written as `"unknown"`** — never
  `true`.
- `navigated: false` concluded from a timeout is `inferred`, every time.
- A `type` outcome's `status: "ok"` is `asserted` unless a later
  `state_read` observes the value in page state. Where the producer
  never reports form values back at all (the current Glazyr producer:
  state is written on load, and no code compares asserted values against
  producer form data), **every fill remains `asserted` for its whole
  life** and ends at an `invalidation`. Emitters in that position say so
  here rather than implying a confirmation path that doesn't exist.
- A re-matched target (`requested_id` ≠ `id`, §5.3) caps the outcome at
  `inferred`.

### 5.5 `invalidation`

Asserted state was dropped. This event is what makes the resume story
sound: without it, a reader cannot tell whether a value an earlier
`action` asserted still stands.

| Field | Required | Meaning |
|---|---|---|
| `reason` | yes | `new_document` \| `producer_dead` \| `session_reset` \| `superseded`. Emitters may extend. `superseded` covers an assertion replaced in place — typing into the same field twice, toggling a checkbox back, a radio click unchecking its siblings — with no document change. |
| `dropped` | yes | Array of assertion references: `{"n": <event index of the assertion>, "desc": ...}`. May be empty when nothing was outstanding. Each reference must name an `action` event earlier in the same file. References are file-global (§4): in a resident emitter whose assertion state is shared, one session's navigation can drop another session's assertions — the event carries the causing session, and `dropped` may point across sessions. |
| `detail` | no | Verbatim supporting text (e.g. the producer's last error). |

**Pending fills are a derived view**: the assertions — `type` actions
and toggle clicks (clicks whose target is a checkbox, radio, or button
control) — with an `ok` or absent outcome, whose `n` appears in **no**
later `invalidation`'s `dropped` list. Actions with an `error` outcome
are not pending (they are failures). Interrupted actions (no outcome)
are included and tagged as interrupted. The view is computed across
sessions when the file has several. Two v1 limits, stated plainly:
observation never clears a fill — no `state_read` field records which
assertions it saw, so a fill leaves the list only by being dropped —
and the resume flow therefore treats pending fills as *unconfirmed
assertions to verify against the live page*, never as values to trust
or to blindly re-type. Readers compute the view; emitters never store
it.

### 5.6 `freshness`

The emitter's liveness view of its state producer changed. Emitters
without a producer omit this kind and leave it out of their
capabilities — **absence means "not monitored", never "always fresh".**

| Field | Required | Meaning |
|---|---|---|
| `status` | yes | `live` \| `dead` \| `unknown` \| `recovered`. `unknown` = no producer has run yet, or liveness has not been probed. (There is deliberately no `stale`: nothing computes it honestly today.) |
| `detail` | no | Verbatim supporting text (error strings, guard verdicts). |

Honesty rules: a `live` verdict from a process-exists probe is
`inferred` provenance — the pid existing is not the renderer answering.
`recovered` may be written **only after a `state_read` observes a new
`epoch`**; a respawn call returning without error is not a recovery.

### 5.7 `note`

The only agent-written kind: intent. The bridge records what happened;
only the agent can record why.

| Field | Required | Meaning |
|---|---|---|
| `text` | yes | The goal, subgoal, or observation, in the agent's words. |
| `label` | no | `goal` \| `subgoal` \| `observation` (suggested vocabulary). |
| `cause` | — | Always `agent`. |
| `provenance` | — | Always `asserted`. |

A resuming agent reads the latest `goal` note first: replay without the
goal produces motion, not progress.

### 5.8 `session_end`

A courtesy marker, written when a session ends cleanly. **Nothing may
depend on it**: the resume case is precisely the case where it was never
written, and every view a reader builds must be derivable from the file
tail without it.

| Field | Required | Meaning |
|---|---|---|
| `reason` | yes | `completed` \| `agent_exit` \| `error`. `completed` may be written only by an emitter that knows the task's state (an agent-side emitter). A bridge-side emitter, which cannot know whether the task finished, writes `agent_exit` or `error`. Nothing in a file records which side an emitter is on, so this rule binds writers; readers cannot check it and must not try. |
| `totals` | no | `{"reads": ..., "actions": ..., "failures": ..., "tokens": {"count": ..., "tokenizer": ...}}` — totals **computed from this session's own events**, or omitted. Instance-wide counters (which in a resident emitter span every client) must never be reported as session totals. A token total without its tokenizer must not be written. |

## 6. Hash chain

- Each event's `prev_hash` is the SHA-256 (lowercase hex) of the
  **previous line in the file**, whatever session it belongs to,
  computed as follows: parse that line as JSON; take the resulting
  object **with every member included, its own `prev_hash` among
  them**; re-serialize it with **RFC 8785 (JSON Canonicalization
  Scheme)**; UTF-8 encode; hash. The first line's `prev_hash` is the
  SHA-256 of the empty byte string
  (`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`).
- **Writers hash the line they actually wrote, parsed back** — not an
  in-memory object that may serialize differently. Because the
  definition is parse-then-canonicalize, it survives re-indentation and
  line-ending conversion (e.g. git autocrlf on Windows).
- Canonicalization is not optional, and it must not be hand-rolled:
  emitters use a conformant RFC 8785 implementation (e.g. the `rfc8785`
  package on PyPI, the `canonicalize` package on npm). Sorted-keys JSON
  is *close* to JCS but differs on number formatting and string
  escaping, and close fails.
- The **head hash** — the SHA-256 of the latest line, computed the same
  way — is the chain's anchor. Readers surface it (the `glazyr_trace`
  tool prints it in its result footer), which lands it in transcripts
  the emitter cannot rewrite. Verification is checkable up to the last
  anchored head; the tail after it is unanchored. That is a known v1
  limit, stated rather than hidden.
- Reader calls (`glazyr_trace` and kin) are **not** trace events and are
  never written to the file, so a footer always shows the head as of
  the moment of the call.
- A reader verifies by recomputing the chain, and the terms are exact:
  - The **verified prefix** is the run of lines, from line 1, that
    parse, carry no mechanically-checkable violation (§3), and whose
    incoming link checks out (line 1 against the genesis hash; line
    k against the hash of line k−1) — **and** whose outgoing link also
    checks out, except for the file's final line, which has no
    successor and is verified on its incoming link alone.
  - A **break at n=k** means line k is the first line *not* in the
    verified prefix because its outgoing link fails (line k+1's
    `prev_hash` ≠ hash of line k), or because line k itself is
    unparseable (then k is its line number). The verified prefix is
    lines 1..k−1, and its head hash is the hash of line k−1. Blaming k
    is a reporting convention, **not a diagnosis**: an edited line k, a
    damaged `prev_hash` in line k+1, and a lost line all present the
    same way, and the chain cannot tell them apart. (A gap in the `n`
    sequence is reported separately, as a conformance violation; it is
    evidence for the lost-line case, nothing stronger.) If line 1's
    `prev_hash` is not the genesis hash, the break is at n=1 and the
    verified prefix is empty.
  - A line with a **duplicate key** is treated as unparseable for chain
    purposes: chain status break at that line, *and* a non-conformance
    reported at that line. Both reports, every time — two readers must
    never disagree on a file's chain status.
  - **Exception — torn tail:** a final line with no trailing LF that
    fails to parse is a *torn write* — reported as truncation, not as a
    chain break, and the verified prefix stands. A final line that
    parses but has no trailing LF is accepted into the prefix with a
    warning (`unterminated final line`).
  - Readers judge the file **as of the moment of the read**. On a live,
    growing file, a partially-appended final line reads as torn; a
    later read may verify it. That is normal operation, not an error.
- The chain detects torn writes, lost lines, concatenated files, and
  out-of-order splices across state epochs. It does not resist an editor
  who recomputes it, and this format must not be described as
  tamper-evident or signed.

## 7. Provenance

| Value | Meaning | Example |
|---|---|---|
| `observed` | Read directly from page/producer state. | A click's navigation, confirmed by a changed `doc_key`. |
| `asserted` | The emitter performed the action and claims its effect, unconfirmed. | A typed value living in the bridge's overlay, not yet seen in page state. |
| `inferred` | Concluded from indirect evidence. | A navigation judged "landed" because the document changed and the host matches, though the URL differs from the target. |

When one event carries claims of differing provenance, the event takes
the weakest of them: an event is only as strong as its least-certain
claim, so `observed` claims must not ride on an event whose other
claims are merely `inferred`. When in doubt, split the event.

## 8. Cause

Provenance says how the emitter *knows*; cause says why the action
*happened*. They are different axes, and replay is a cause, not a
provenance.

| Value | Meaning |
|---|---|
| `agent` | The consuming agent requested this action. |
| `replay` | The action was re-performed from a trace during a resume. |
| `bridge_recovery` | The emitter acted on its own to recover: relaunching a dead browser and retrying a navigation, scrolling a target into view, escalating a click through its fallback chain. |

Bridge-initiated actions are real actions. A trace that hides them
makes the agent's run look cleaner than it was and the emitter's
behavior impossible to debug.

## 9. Capabilities and absence

- `session_start.capabilities` is the contract every absence in the
  session is read against. The contract is about **kinds**: a reader
  that sees no `freshness` events checks the declaration — kind not
  declared → not monitored; kind declared but absent where the view
  needs it → a gap worth flagging. The `fields` list carries no such
  contract (§5.1): it documents what may appear, and absence of a field
  is read per-field (an emitter that can't know it omits it), never as
  a capabilities breach.
- An emitter must never write a field it cannot honestly fill — no
  placeholder `null`, `0`, or `""`. In particular, a stack without a
  state producer omits `state.epoch` and `state.seq` rather than
  inventing them, and a stack that cannot hash a state omits `hashes`
  rather than hashing something incomparable.
- Fields a stack *can* fill honestly in its own terms, it fills in its
  own terms, qualified: its own snapshot hash with its own `basis`, its
  own node reference with its own `id_scope`, its own target signature
  under its own `scheme`.

## 10. Redaction

- Emitters must not write credential values, cookies, authorization
  headers, or typed values from credential-bearing fields (password
  inputs and their kin) into a trace. The event records the action and
  `value_redacted: true`; the value itself never lands.
- Beyond credentials: a trace is a record of where an agent went and
  what it did. Treat the file as sensitive even when redaction was
  applied — it lives locally, under the owner's control, and this
  format's threat model (§2, §6) does not include protecting it from
  its owner.

## 11. Forward compatibility

- Every event carries `v`. This document defines `v: 1`.
- **Readers must ignore unknown kinds and unknown fields.** A trace
  written against a later minor revision of this format remains readable
  by earlier readers, minus the parts they don't know.
- Within a major version, a field name never changes meaning. A breaking
  change bumps `v`.
- Emitters may begin writing before a reader exists for every field —
  Swivel, the first non-Glazyr emitter, starts at the Agentware kickoff
  (2026-11-27) against this draft; its traces stay valid as the format
  settles.

## 12. Example

Illustrative excerpt (hashes truncated with `…` for readability; real
traces carry full digests). Note the write-ahead shape: each `action`
is followed by its `action_outcome`.

```jsonl
{"v":1,"n":1,"ts":"2026-11-27T18:04:31.207Z","kind":"session_start","session":"swivel-run-1","prev_hash":"e3b0c442…","provenance":"asserted","emitter":{"name":"swivel","version":"0.1.0"},"stack":"playwright-mcp","capabilities":{"kinds":["session_start","state_read","action","action_outcome","note","session_end"],"fields":["url","target.sig","tokens"]}}
{"v":1,"n":2,"ts":"2026-11-27T18:04:31.310Z","kind":"note","session":"swivel-run-1","prev_hash":"9f2c…","provenance":"asserted","cause":"agent","label":"goal","text":"Reconcile October vendor invoices against ledger.csv; file one dispute for the largest overbilling."}
{"v":1,"n":3,"ts":"2026-11-27T18:04:32.014Z","kind":"action","session":"swivel-run-1","prev_hash":"b81a…","provenance":"asserted","cause":"agent","action":"navigate","url":"https://vendor-a.example/invoices"}
{"v":1,"n":4,"ts":"2026-11-27T18:04:33.120Z","kind":"action_outcome","session":"swivel-run-1","prev_hash":"77aa…","provenance":"inferred","ref":3,"outcome":{"status":"ok","navigated":true}}
{"v":1,"n":5,"ts":"2026-11-27T18:04:33.551Z","kind":"state_read","session":"swivel-run-1","prev_hash":"04d7…","provenance":"observed","url":"https://vendor-a.example/invoices","hashes":{"basis":"pw-aria-snapshot","state":"77be…"},"tokens":{"count":1893,"tokenizer":"cl100k"}}
{"v":1,"n":6,"ts":"2026-11-27T18:04:35.902Z","kind":"action","session":"swivel-run-1","prev_hash":"1c9e…","provenance":"asserted","cause":"agent","action":"type","target":{"desc":"Invoice number filter","sig":{"scheme":"pw-role-name","role":"textbox","name":"Invoice number"}},"value":"INV-2041"}
{"v":1,"n":7,"ts":"2026-11-27T18:04:36.205Z","kind":"action_outcome","session":"swivel-run-1","prev_hash":"5d01…","provenance":"asserted","ref":6,"outcome":{"status":"ok"}}
```

Note what the example demonstrates: a Playwright-stack emitter with no
`state.epoch`/`seq`, no `doc_key`, no `freshness` — and no placeholders
standing in for them. Its navigation outcome is `inferred` (URL
comparison), its typed value is `asserted` (unconfirmed), and both facts
survive in the record.

## 13. The `glazyr_trace` tool contract

The reader tool is part of the format: a trace nobody can query cheaply
is a write-only archive. This section is normative for any tool that
presents itself as a Glazyr Trace reader.

### 13.1 Interface

`glazyr_trace(mode, file?, session?, url?, since?, limit?)`

| Parameter | Meaning |
|---|---|
| `mode` | `summary` (default) \| `tail` \| `failures` \| `url` \| `since` \| `verify` \| `note`. |
| `file` | Trace file to read. Default: the emitter's current file. The resume case names a *predecessor's* file — a fresh instance reading a dead one's trace — so `file` is a first-class parameter, not a setting. |
| `session` | Restrict views to one session. Default: the caller's session if the tool knows it, else the most recent session in the file. |
| `url` | For `url` mode: the URL to report on. |
| `since` | For `since` mode: an event index `n`; report what changed after it. |
| `limit` | Maximum events to return before truncation (§13.4). |

Query modes never write to the file (§6). `note` mode is the single
exception: it appends exactly one `note` event (§5.7) on the agent's
behalf and returns its `n`. One tool, because the note is trace-shaped
and a second tool would compete for the agent's attention; the write is
confined to that mode so the read paths stay side-effect-free.

### 13.2 Modes

- **`summary`** — the no-args default. Session count, event counts by
  kind, reads/actions/failures totals, token totals (tokenizer named),
  the latest goal note, current position, and the footer (§13.3).
- **`tail`** — the resume call. Contents, in this order:
  1. The latest `goal` note.
  2. **Any interrupted action** (an `action` with no `action_outcome`),
     by name, before everything else after the goal: it is the one
     fact that can cause a double-submit.
  3. Current position: last `state_read`'s URL, `doc_key`, and
     `state` reference.
  4. Live comparison, when the tool runs inside a live emitter:
     `live_state: matches | differs | producer_restarted |
     no_live_browser` — the trace's last `epoch`/`seq` against the
     running producer. This answers which of the two resume cases
     applies — the browser survived (resume in place) or it didn't
     (re-navigate and replay) — without the agent diffing two tool
     outputs by eye.
  5. Pending fills (derived, §5.5), with signatures.
  6. The last actions (default 10) with outcomes and provenance, and a
     per-step token column.
- **`failures`** — every `action_outcome` with `status: "error"`, every
  `state_read` with `error`, and every `freshness` death, each with the
  `state_read` immediately before it. Failure texts are verbatim and
  are never truncated (§13.4).
- **`url`** — the latest `state_read` for the URL, plus every action
  taken on it: "have I been here, and what did I do."
- **`since`** — change since event `n`: documents visited, `doc_key`
  changes, structure/outline changes (derived per §5.2, and reported
  as *unavailable* for any pair whose hash bases differ), actions
  taken, failures. This is the "what changed while I wasn't looking"
  call.
- **`verify`** — the chain report (§13.5) on its own.

When a session's `session_start` links a predecessor file
(`continues`), views report the link (file + head hash) in the footer;
following it is the caller's choice via `file`.

### 13.3 The footer

Every response ends with a footer carrying:

- The response's own token cost, with the tokenizer named.
- The volume summarized, where meaningful: reads and tokens the view
  stands in for ("38 reads / 52,790 tokens → this response").
- The current **head hash** (§6) — the anchor that lands the chain in
  the client transcript.
- The chain status (§13.5): `chain: ok` \| `chain: break at n=<k>` \|
  `chain: torn tail`.
- Capability gaps that bear on the view, stated plainly: e.g.
  `freshness: not monitored by this emitter` — never silence where the
  format promises a signal.

### 13.4 Cost law and truncation

- **A trace call must cost less than the reads it saves.** The `tail`
  (resume) call budgets ~300–500 tokens. Other modes budget
  proportionally to what they replace.
- Truncation cuts in this order: hash values first; full hashes →
  changed-flags; collapse unchanged stub reads; token detail last.
  Truncated lists end with a `… +N more` marker.
- **Never cut, at any budget:** target signatures, provenance values,
  failure texts, the latest goal note, and the interrupted-action
  report. A summary that saves tokens by dropping the one dangerous
  fact has the economics exactly backwards.

### 13.5 Chain verification

Every mode verifies the chain as it reads (verification is a byproduct
of reading, not a separate chore) and reports status in the footer:

- `ok` — every line parsed and every `prev_hash` matched.
- `break at n=<k>` — as defined in §6: line k is the first line outside
  the verified prefix. Views are still served from the verified prefix,
  and the response says so.
- `torn tail` — the final line is incomplete (no trailing LF, fails to
  parse): a writer died mid-append. The verified prefix is the trace;
  the torn line is reported, never silently swallowed.

`verify` mode returns the full report: events verified, first break
position, and the head hash of the verified prefix.

### 13.6 Errors and unknowns

- Missing or empty file: a plain statement of that fact, not an
  exception trace.
- Events with a `v` newer than the reader: the reader serves what it
  understands (unknown kinds and fields ignored, §11) and says the
  file's version in the footer. Conformance checks in this document
  are defined for `v: 1` events; newer events are chain-verified and
  passed through, not conformance-checked.
- A view that cannot be computed from the file (e.g. `since` across
  incomparable hash bases) reports itself *unavailable* with the
  reason. It never fabricates a plausible-looking answer — the tool
  obeys the same honesty rules as the format it reads.
