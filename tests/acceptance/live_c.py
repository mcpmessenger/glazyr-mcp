#!/usr/bin/env python3
"""Acceptance (c) orchestrator. Run inside ns.sh:
    RUN=c1 tests/acceptance/ns.sh /root/.glazyr/venv/bin/python tests/acceptance/live_c.py 1
    RUN=c2 tests/acceptance/ns.sh /root/.glazyr/venv/bin/python tests/acceptance/live_c.py 2
GLAZYR_TEST_PKG picks the package under test (e.g. a 0.3.3 baseline copy).
  phase 1 (resident): kill the producer mid-session; then kill client A2
                      mid-click (the resident is not in its tree - the
                      outcome still lands).
  phase 2 (private bridge, GLAZYR_RESIDENT=off): kill client B's whole
                      process tree mid-click (mcporter-style tree retire);
                      the emitter dies with it, and a fresh instance C
                      reads B's trace with glazyr_trace(mode=tail, file=...).
"""
import glob, json, os, signal, subprocess, sys, threading, time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, 'fixtures')
BASE = 'http://127.0.0.1:8950'
PY = sys.executable
WORKER = os.path.join(HERE, 'worker.py')
TRACES = '/root/.glazyr/traces'


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=FIX, **k)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith('/slow'):
            time.sleep(25)
            self.path = '/done.html'
        return super().do_GET()


srv = ThreadingHTTPServer(('127.0.0.1', 8950), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def log(*a):
    print('[orch]', *a, flush=True)


def procs():
    out = []
    for p in os.listdir('/proc'):
        if p.isdigit():
            try:
                cmd = open(f'/proc/{p}/cmdline', 'rb').read().replace(b'\0', b' ').decode(errors='ignore')
                ppid = int(open(f'/proc/{p}/stat').read().rsplit(')', 1)[1].split()[1])
                out.append((int(p), ppid, cmd))
            except Exception:
                pass
    return out


def kill_producer():
    # "The producer" exactly as the bridge defines it (find_sem): the
    # headless_shell process holding the 4 MiB state memfd.
    victims = []
    for p, _, c in procs():
        if 'headless_shell' not in c:
            continue
        try:
            for fd in os.listdir(f'/proc/{p}/fd'):
                path = f'/proc/{p}/fd/{fd}'
                if 'memfd' in os.readlink(path) and os.stat(path).st_size == 4 * 1024 * 1024:
                    victims.append(p)
                    break
        except Exception:
            pass
    for p in victims:
        try:
            os.kill(p, signal.SIGKILL)
        except Exception:
            pass
    log(f'SIGKILLed producer processes: {victims}')


def kill_renderers():
    """Wedge the producer the way the Nov 21 incident did: the browser
    process (and CDP endpoint) lives, the page's renderer is gone, seq
    freezes. Only the guard's renderer probe can see this."""
    victims = [p for p, _, c in procs() if 'headless_shell' in c and '--type=renderer' in c]
    for p in victims:
        try:
            os.kill(p, signal.SIGKILL)
        except Exception:
            pass
    log(f'SIGKILLed renderer processes: {victims} (browser main + CDP left alive)')


def descendants(root):
    ps = procs()
    kids, frontier = [], [root]
    while frontier:
        nxt = [p for p, pp, _ in ps if pp in frontier]
        kids += nxt
        frontier = nxt
    return kids


def run_worker(steps, env_extra=None, kill_at=None, kill_mode=None, label=''):
    env = dict(os.environ, **(env_extra or {}))
    proc = subprocess.Popen([PY, WORKER, json.dumps(steps)], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, env=env)
    for line in proc.stdout:
        print(f'  {label}| {line.rstrip()}'[:400], flush=True)
        if line.startswith('@@PAUSE') and kill_mode == 'producer':
            kill_producer()
        if line.startswith('@@PAUSE') and kill_mode == 'renderer':
            kill_renderers()
        if kill_at is not None and line.startswith(f'@@STEP {kill_at} '):
            time.sleep(3)
            if kill_mode == 'client':
                log(f'SIGKILL client {label} (pid {proc.pid}) 3s into step {kill_at}; '
                    f'its server side is left alone')
                proc.kill()
            elif kill_mode == 'tree':
                tree = descendants(proc.pid)
                log(f'SIGTERM client {label} and its whole process tree '
                    f'{[proc.pid] + tree} 3s into step {kill_at}')
                for p in [proc.pid] + tree:
                    try:
                        os.kill(p, signal.SIGTERM)
                    except Exception:
                        pass
            break
    try:
        for line in proc.stdout.read().splitlines():
            print(f'  {label}| {line}'[:400], flush=True)
    except Exception:
        pass
    proc.wait()


GRAB_LOGIN = {'user': r"\[(\d+)\] input INPUT_TEXT name='user'",
              'pw': r"\[(\d+)\] input INPUT_PASSWORD name='password'",
              'slowlink': r"Slow link \[(\d+)\]"}

phase = sys.argv[1]
if phase == '1':
    log('=== phase 1: resident; kill the producer mid-session ===')
    run_worker([
        {'tool': 'glazyr_trace', 'args': {'mode': 'note', 'label': 'goal',
                                          'text': 'Sign in as ada; survive a browser crash.'}},
        {'tool': 'glazyr_navigate', 'args': {'url': f'{BASE}/login.html'}},
        {'tool': 'glazyr_peek', 'grab': GRAB_LOGIN},
        {'tool': 'glazyr_type', 'args': {'node_id': '$user', 'text': 'ada'}},
        {'tool': 'glazyr_type', 'args': {'node_id': '$pw', 'text': 'pw-should-never-show'}},
        {'pause': 52},                      # renderer killed; seq frozen past the 45s probe threshold
        {'tool': 'glazyr_type', 'args': {'node_id': '$user', 'text': 'late'}},  # guard: GLAZYR_PRODUCER_DEAD
        {'tool': 'glazyr_navigate', 'args': {'url': f'{BASE}/login.html'}},
        {'tool': 'glazyr_peek', 'args': {'force': True}, 'grab': GRAB_LOGIN},  # fix 6 proof
        {'tool': 'glazyr_type', 'args': {'node_id': '$user', 'text': 'bob'}},
        {'tool': 'glazyr_trace', 'args': {'mode': 'failures'}},
    ], kill_mode='renderer', label='A1')
    log('=== phase 1b: resident; kill client A2 mid-click (bridge not in its tree) ===')
    run_worker([
        {'tool': 'glazyr_peek', 'grab': GRAB_LOGIN},
        {'tool': 'glazyr_click', 'args': {'node_id': '$slowlink'}},
    ], kill_at=1, kill_mode='client', label='A2')
    time.sleep(14)   # the resident finishes the click on its own
    log('=== phase 1c: resident; a new client reads the tail ===')
    run_worker([
        {'tool': 'glazyr_trace', 'args': {'mode': 'tail', 'session': '*'}},
    ], label='A3')
elif phase == '2':
    log('=== phase 2: private bridge (GLAZYR_RESIDENT=off); kill client B tree mid-click ===')
    run_worker([
        {'tool': 'glazyr_trace', 'args': {'mode': 'note', 'label': 'goal',
                                          'text': 'Follow the slow link to the done page, once.'}},
        {'tool': 'glazyr_navigate', 'args': {'url': f'{BASE}/login.html'}},
        {'tool': 'glazyr_peek', 'grab': GRAB_LOGIN},
        {'tool': 'glazyr_type', 'args': {'node_id': '$user', 'text': 'carol'}},
        {'tool': 'glazyr_click', 'args': {'node_id': '$slowlink'}},
    ], env_extra={'GLAZYR_RESIDENT': 'off'}, kill_at=4, kill_mode='tree', label='B')
    time.sleep(2)
    files = sorted(glob.glob(f'{TRACES}/*.trace.jsonl'), key=os.path.getmtime)
    dead = files[-1]
    log(f'client B trace file: {os.path.basename(dead)}; last line:')
    print('   ', open(dead, encoding='utf-8').read().splitlines()[-1][:300], flush=True)
    log("=== phase 2b: fresh private instance C resumes from B's file ===")
    run_worker([
        {'tool': 'glazyr_trace', 'args': {'mode': 'note', 'label': 'observation',
                                          'text': 'Fresh instance; reading the dead session before acting.'}},
        {'tool': 'glazyr_trace', 'args': {'mode': 'tail', 'file': dead}},
        {'tool': 'glazyr_trace', 'args': {'mode': 'verify', 'file': dead}},
    ], env_extra={'GLAZYR_RESIDENT': 'off'}, label='C')
    files = sorted(glob.glob(f'{TRACES}/*.trace.jsonl'), key=os.path.getmtime)
    log(f'instance C file: {os.path.basename(files[-1])}; first line:')
    print('   ', open(files[-1], encoding='utf-8').readline()[:700], flush=True)
srv.shutdown()
