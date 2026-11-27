#!/usr/bin/env python3
"""Reference checker for the TRACE_FORMAT conformance fixtures.

Implements the reader rules of TRACE_FORMAT §3/§6 (chain verification,
torn-tail exception, duplicate-key and null prohibitions) and asserts
each fixture's expected verdict from README.md.

Run:  ~/workspace/.venv-trace/bin/python check_fixtures.py
"""
import hashlib
import json
import os
import sys

import rfc8785

HERE = os.path.dirname(os.path.abspath(__file__))
GENESIS = hashlib.sha256(b"").hexdigest()


def load_lines(name):
    raw = open(os.path.join(HERE, name), "rb").read().decode("utf-8")
    ends_with_lf = raw.endswith("\n")
    lines = raw.split("\n")
    if ends_with_lf:
        lines = lines[:-1]
    return lines, ends_with_lf


def parse_strict(line):
    """Parse one line, detecting duplicate keys. Returns (obj, dup_key)."""
    dups = []

    def hook(pairs):
        seen = set()
        for k, _ in pairs:
            if k in seen:
                dups.append(k)
            seen.add(k)
        return dict(pairs)

    return json.loads(line, object_pairs_hook=hook), (dups[0] if dups else None)


def find_nulls(obj, path=""):
    hits = []
    if obj is None:
        hits.append(path or "<root>")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            hits += find_nulls(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += find_nulls(v, f"{path}[{i}]")
    return hits


def check(name):
    """Returns a verdict dict for one fixture file."""
    lines, ends_lf = load_lines(name)
    verdict = {"file": name, "chain": None, "verified": 0, "notes": []}
    prev_obj = None
    events = []
    for idx, line in enumerate(lines, start=1):
        is_last = idx == len(lines)
        try:
            obj, dup = parse_strict(line)
        except json.JSONDecodeError:
            if is_last and not ends_lf:
                verdict["chain"] = "torn tail"
                verdict["notes"].append(f"line {idx}: torn write (no LF, unparseable)")
            else:
                verdict["chain"] = f"break at line {idx}"
                verdict["notes"].append(f"line {idx}: unparseable")
            break
        if dup is not None:
            verdict["chain"] = f"non-conformant at line {idx}"
            verdict["notes"].append(f"line {idx}: duplicate key {dup!r}")
            break
        nulls = find_nulls(obj)
        if nulls:
            verdict.setdefault("violations", []).append(
                f"n={obj.get('n')}: null value at {nulls[0]}")
        expected_prev = GENESIS if prev_obj is None else hashlib.sha256(
            rfc8785.dumps(prev_obj)).hexdigest()
        if obj.get("prev_hash") != expected_prev:
            # §6 (v0.4): the blamed line is the first UNTRUSTED line —
            # its outgoing link failed, so it leaves the verified prefix.
            verdict["chain"] = f"break at n={prev_obj.get('n') if prev_obj else idx - 1}"
            verdict["notes"].append(
                f"line {idx}: prev_hash does not match hash of line {idx - 1}")
            verdict["verified"] = idx - 2
            break
        events.append(obj)
        verdict["verified"] = idx
        prev_obj = obj
    else:
        verdict["chain"] = "ok"
    verdict["events"] = events
    # derived views
    outcomes = {e.get("ref") for e in events if e.get("kind") == "action_outcome"}
    verdict["interrupted"] = [e["n"] for e in events
                              if e.get("kind") == "action" and e["n"] not in outcomes]
    bases = [e["hashes"]["basis"] for e in events
             if e.get("kind") == "state_read" and "hashes" in e]
    verdict["hash_bases"] = bases
    return verdict


EXPECT = {
    "valid-basic.trace.jsonl":
        {"chain": "ok", "verified": 12, "interrupted": []},
    "valid-swivel.trace.jsonl":
        {"chain": "ok", "verified": 8, "interrupted": []},
    "valid-interrupted.trace.jsonl":
        {"chain": "ok", "verified": 6, "interrupted": [6]},
    "valid-incomparable-hashes.trace.jsonl":
        {"chain": "ok", "verified": 3,
         "hash_bases": ["glazyr-apc-standard", "pw-aria-snapshot"]},
    "invalid-torn-tail.trace.jsonl":
        {"chain": "torn tail", "verified": 5},
    "invalid-broken-chain.trace.jsonl":
        {"chain": "break at n=5", "verified": 4},
    "invalid-null-placeholder.trace.jsonl":
        {"chain": "ok", "verified": 3,
         "violations": ["n=3: null value at doc_key"]},
    "invalid-duplicate-key.trace.jsonl":
        {"chain": "non-conformant at line 3", "verified": 2},
}

failures = 0
for name, exp in EXPECT.items():
    v = check(name)
    got = {k: v[k] for k in exp}
    ok = got == exp
    failures += 0 if ok else 1
    print(("PASS " if ok else "FAIL ") + name)
    print(f"     chain={v['chain']} verified={v['verified']} "
          f"interrupted={v['interrupted']}"
          + (f" violations={v.get('violations')}" if v.get("violations") else "")
          + (f" bases={v['hash_bases']}" if v["hash_bases"] else ""))
    if not ok:
        print(f"     expected {exp}\n     got      {got}")
    for note in v["notes"]:
        print(f"     note: {note}")

print("\n%d/%d fixtures match their expected verdicts"
      % (len(EXPECT) - failures, len(EXPECT)))
sys.exit(1 if failures else 0)
