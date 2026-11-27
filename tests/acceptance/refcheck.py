#!/usr/bin/env python3
"""Judge trace files with the reference checker's own check()
(trace-fixtures/check_fixtures.py), without running its fixture loop.
Usage: refcheck.py <file.trace.jsonl>...   exit 1 unless every chain is ok."""
import collections, os, sys
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'trace-fixtures')
src = open(os.path.join(FIX, 'check_fixtures.py'), encoding='utf-8').read().split('EXPECT = {')[0]
ns = {'__file__': os.path.join(FIX, 'check_fixtures.py')}
exec(compile(src, 'check_fixtures.py', 'exec'), ns)
bad = 0
for f in sys.argv[1:]:
    v = ns['check'](os.path.abspath(f))
    kinds = dict(collections.Counter(e['kind'] for e in v['events']))
    print(f"{os.path.basename(f)}: chain={v['chain']} verified={v['verified']} "
          f"interrupted={v['interrupted']} violations={v.get('violations', [])} kinds={kinds}")
    bad += v['chain'] != 'ok'
sys.exit(1 if bad else 0)
