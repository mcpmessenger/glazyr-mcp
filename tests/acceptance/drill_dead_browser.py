#!/usr/bin/env python3
"""Repro drill: dead browser served as live.

Kills the WHOLE browser (SIGKILL every headless_shell, the real crash)
between calls, N times, and checks the first call after each death:
odd iterations a read (glazyr_peek), even iterations an action
(glazyr_type). Before the fix, ensure_browser() replaced the dead
browser before the guard looked, and the dead page (and its typed
overlay) could be served as live, or typed into by stale node id.

Pass criteria, per death:
  - the first call answers GLAZYR_PRODUCER_DEAD;
  - it serves no state body from the dead page and no stale overlay;
  - trace: invalidation{producer_dead} drops that iteration's type, and
    no successful state_read after the death carries the dead epoch.

Run:  RUN=drill-dead-browser tests/acceptance/ns.sh \
          /root/.glazyr/venv/bin/python tests/acceptance/drill_dead_browser.py [N]
"""
import glob, json, os, signal, subprocess, sys, threading, time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, 'fixtures')
BASE = 'http://127.0.0.1:8950'
N = int(sys.argv[1]) if len(sys.argv) > 1 else 4
TRACES = '/root/.glazyr/traces'


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=FIX, **k)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(('127.0.0.1', 8950), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def kill_browser():
    victims = []
    for p in os.listdir('/proc'):
        if p.isdigit():
            try:
                if b'headless_shell' in open(f'/proc/{p}/cmdline', 'rb').read():
                    os.kill(int(p), signal.SIGKILL)
                    victims.append(int(p))
            except Exception:
                pass
    print(f'[drill] SIGKILLed whole browser: {victims}', flush=True)


GRAB = {'user': r"\[(\d+)\] input INPUT_TEXT name='user'"}
steps, checks = [], []
for i in range(1, N + 1):
    steps += [
        {'tool': 'glazyr_navigate', 'args': {'url': f'{BASE}/login.html'}},
        {'tool': 'glazyr_peek', 'args': {'force': True}, 'grab': GRAB},
        {'tool': 'glazyr_type', 'args': {'node_id': '$user', 'text': f'ada{i}'}},
        {'pause': 2},
    ]
    probe = ({'tool': 'glazyr_peek'} if i % 2 else
             {'tool': 'glazyr_type', 'args': {'node_id': '$user', 'text': f'late{i}'}})
    checks.append((len(steps), i, probe['tool']))
    steps.append(probe)

proc = subprocess.Popen([sys.executable, os.path.join(HERE, 'worker.py'), json.dumps(steps)],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
responses, cur, buf = {}, None, []
for line in proc.stdout:
    print('  |', line.rstrip()[:300], flush=True)
    if line.startswith('@@PAUSE'):
        kill_browser()
    elif line.startswith('@@STEP'):
        cur, buf = int(line.split()[1]), []
    elif line.startswith('@@DONE'):
        responses[cur] = '\n'.join(buf)
    else:
        buf.append(line.rstrip('\n'))
proc.wait()
srv.shutdown()

print('\n===== DRILL RESULTS =====')
fails = 0
for idx, i, tool in checks:
    r = responses.get(idx, '')
    problems = []
    if 'GLAZYR_PRODUCER_DEAD' not in r:
        problems.append('no GLAZYR_PRODUCER_DEAD')
    if '# url: http://127.0.0.1:8950/login.html' in r and tool == 'glazyr_peek':
        problems.append('served a page body after the death')
    if f"value='ada{i}'" in r:
        problems.append(f"stale overlay value='ada{i}' served")
    if tool == 'glazyr_type' and 'typed' in r:
        problems.append('typed into the new browser by a stale node id')
    fails += bool(problems)
    print(f"death {i} ({tool}): {'PASS' if not problems else 'FAIL - ' + '; '.join(problems)}")
    print(f"    first response: {r.splitlines()[0][:220] if r else '(none)'}")

f = sorted(glob.glob(f'{TRACES}/*.trace.jsonl'), key=os.path.getmtime)[-1]
ev = [json.loads(l) for l in open(f, encoding='utf-8')]
types = [e for e in ev if e['kind'] == 'action' and e.get('action') == 'type'
         and str(e.get('value', '')).startswith('ada')]
inv = [e for e in ev if e['kind'] == 'invalidation' and e['reason'] == 'producer_dead']
dropped = {d['n'] for e in inv for d in e['dropped']}
stale, dead_epochs = [], set()
last_epoch = None
for e in ev:
    if e['kind'] == 'state_read' and 'error' not in e:
        ep = (e.get('state') or {}).get('epoch')
        if ep in dead_epochs:
            stale.append(e['n'])
        last_epoch = ep
    # a death marker: freshness dead, or the bridge relaunching the browser
    if last_epoch and ((e['kind'] == 'freshness' and e['status'] == 'dead') or
                       (e['kind'] == 'action' and e.get('action') == 'relaunch_browser')):
        dead_epochs.add(last_epoch)
undropped = [t['n'] for t in types if t['n'] not in dropped]
print(f'trace {os.path.basename(f)}: deaths(freshness dead)={sum(1 for e in ev if e["kind"] == "freshness" and e["status"] == "dead")} '
      f'producer_dead invalidations={len(inv)}')
print(f'  stale reads after death (dead epoch served): {stale or "none"}')
print(f'  typed assertions not dropped as producer_dead: {undropped or "none"}')
fails += bool(stale) + bool(undropped)
print('DRILL', 'PASS' if not fails else f'FAIL ({fails} problem groups)')
sys.exit(1 if fails else 0)
