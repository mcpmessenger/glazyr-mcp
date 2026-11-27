#!/usr/bin/env python3
"""One line per event, hashes/capabilities elided. Usage: dump_trace.py <file>..."""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'bridge'))
import glazyr_trace as gt
for f in sys.argv[1:]:
    print('==', f, gt.chain_label(gt.verify_file(f)))
    for l in open(f, encoding='utf-8'):
        e = json.loads(l)
        x = {k: v for k, v in e.items() if k not in ('v', 'ts', 'prev_hash', 'capabilities', 'hashes',
                                                     'counts', 'n', 'kind', 'emitter', 'stack', 'title')}
        if 'target' in x:
            x['target'] = x['target'].get('desc')
        print(f"  n={e['n']:>2} {e['kind']:<15} {json.dumps(x, ensure_ascii=False)[:230]}")
