# TRACE_FORMAT conformance fixtures

Golden traces for [TRACE_FORMAT.md](../TRACE_FORMAT.md). Each file has
exactly one expected verdict; a reader implementation conforms when it
produces these verdicts for these files. All chains are real — computed
with the `rfc8785` package per §6 — and are re-verified by
`check_fixtures.py`, which also asserts every verdict below.

Regenerate (after editing `gen_fixtures.py`) and re-check with:

```
~/workspace/.venv-trace/bin/python gen_fixtures.py
~/workspace/.venv-trace/bin/python check_fixtures.py
```

## Valid traces

**`valid-basic.trace.jsonl`** — a full Glazyr session, 12 events:
session start, goal note, navigate (observed outcome), two reads of one
document (the second a stub whose only hash change is `weak` — cosmetic
churn), a type (asserted), a click that navigates, the resulting
`invalidation` dropping the type assertion, and a clean session end.
*Expected: chain ok, 12 verified, no interrupted actions, pending fills
empty after n=10 (the type at n=6 was dropped), change between the two
reads = cosmetic only.*

**`valid-swivel.trace.jsonl`** — a Playwright-stack (Swivel) session, 8
events. No `state`, no `doc_key`, no `freshness`, no placeholders.
Navigation outcome is `inferred`; the session end may say `completed`
because this emitter is agent-side. *Expected: chain ok, 8 verified.*

**`valid-interrupted.trace.jsonl`** — a session whose writer died
mid-task: the last event is a click on "Submit dispute" with **no**
`action_outcome` and no `session_end`. *Expected: chain ok, 6 verified;
the resume view (`tail`) must report action n=6 as interrupted — its
effect is unknown and must not be blindly repeated.*

**`valid-incomparable-hashes.trace.jsonl`** — two reads of the same
document whose `hashes.basis` differs (`glazyr-apc-standard`, then
`pw-aria-snapshot`). *Expected: chain ok, 3 verified; change detection
across that pair is **unavailable** — a reader must not compare the
digests and must not report "changed" or "unchanged" from them.*

## Invalid traces

**`invalid-torn-tail.trace.jsonl`** — 5 valid events, then a 6th line
cut mid-string with no trailing LF (the writer died during the append).
*Expected: chain status **torn tail**, verified prefix = 5. Not a chain
break (§6).*

**`invalid-broken-chain.trace.jsonl`** — 7 events; the URL in event 5
was edited after the chain was computed. *Expected: chain status
**break at n=5** (line 6's `prev_hash` no longer matches line 5), and
the verified prefix is **4** — per §6 the blamed line is the first
untrusted line and is excluded from views. (This count was pinned down
by the Nov 26 stranger test: two independent readers initially
disagreed, 5 vs 4, and the spec now defines it.)*

**`invalid-null-placeholder.trace.jsonl`** — the chain is valid, but
event 3 writes `"doc_key": null`, which §4 forbids (absent is never
null). *Expected: chain ok; conformance violation reported at n=3 —
the trace is non-conformant even though its chain verifies.*

**`invalid-duplicate-key.trace.jsonl`** — line 3 carries the key
`"kind"` twice, which §3 forbids (parsers resolve duplicates silently
and differently, so canonicalization is parser-dependent past that
line). *Expected: non-conformant at line 3; verified prefix = 2.*
