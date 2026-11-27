#!/usr/bin/env python3
"""Drill: a launcher never attaches to a resident of another version.

Inside one namespace (shared resident home), with three real packages:
  P33 - the 0.3.3 source (git archive main): pre-handshake resident;
  P34 - the package under test ($GLAZYR_TEST_PKG);
  P35 - P34 relabelled 0.3.5 (package.json only).
Each step is one MCP client (worker.py -> launcher) calling glazyr_status.

  1. P33 starts a 0.3.3 resident.
  2. P34 must NOT attach: it replaces the 0.3.3 resident (no handshake).
  3. P34 again: attaches to its own resident (same pid, no replace).
  4. P35 must NOT attach to the 0.3.4 resident: replaces it.
  5. Unreachable lock holder (the state an old launcher's reclaim leaves:
     resident.json + socket deleted, process alive holding the lock):
     P34 must reclaim it by /proc scan and start its own resident -
     not wait 30 s and fall back to a private bridge.
  6. Info only: a 0.3.3 launcher has no handshake; what it does against
     a 0.3.4 resident is recorded, not asserted (cannot be fixed from
     the new side).

Run:  RUN=drill-version tests/acceptance/ns.sh \
          /root/.glazyr/venv/bin/python tests/acceptance/drill_version_handshake.py
"""
import json, os, re, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..'))
P34 = os.environ.get('GLAZYR_TEST_PKG', '/root/glazyr-034-install/node_modules/@glazyr/mcp')
P33 = '/tmp/glazyr-drill-pkg-033'
P35 = '/tmp/glazyr-drill-pkg-035'
RES = '/root/.glazyr'

shutil.rmtree(P33, ignore_errors=True)
os.makedirs(P33)
subprocess.run(f'git -C {REPO} archive main launcher.js package.json bridge | tar -x -C {P33}',
               shell=True, check=True)
shutil.rmtree(P35, ignore_errors=True)
shutil.copytree(P34, P35)
pj = json.load(open(f'{P35}/package.json'))
pj['version'] = '0.3.5'
json.dump(pj, open(f'{P35}/package.json', 'w'))
for p in (P33, P34, P35):
    print(f'[drill] package {p}: version {json.load(open(p + "/package.json"))["version"]}', flush=True)


def alive(pid):
    try:
        os.kill(pid, 0)
        return not open(f'/proc/{pid}/stat').read().rsplit(')', 1)[1].split()[0] == 'Z'
    except Exception:
        return False


def resident_json():
    try:
        return json.load(open(f'{RES}/resident.json'))
    except Exception:
        return {}


def call(pkg, label):
    t0 = time.time()
    r = subprocess.run([sys.executable, os.path.join(HERE, 'worker.py'),
                        json.dumps([{'tool': 'glazyr_status'}])],
                       env=dict(os.environ, GLAZYR_TEST_PKG=pkg),
                       capture_output=True, text=True, timeout=180)
    secs = time.time() - t0
    m = re.search(r'bridge_mode: (\w+)(?: \(pid (\d+))?', r.stdout)
    mode, pid = (m.group(1), int(m.group(2)) if m.group(2) else None) if m else (None, None)
    launcher_log = [l for l in r.stderr.splitlines() if '[glazyr]' in l]
    rj = resident_json()
    print(f'[drill] {label}: {secs:.1f}s mode={mode} status_pid={pid} '
          f'resident.json={rj.get("version")}/{rj.get("pid")}', flush=True)
    for l in launcher_log:
        print(f'          launcher: {l}', flush=True)
    return {'mode': mode, 'pid': pid, 'log': '\n'.join(launcher_log), 'json': rj, 'secs': secs}


results = []


def check(name, ok, detail=''):
    results.append(ok)
    print(f'ASSERT {name}: {"PASS" if ok else "FAIL"} {detail}', flush=True)


r1 = call(P33, '1. 0.3.3 launcher')
check('1 a 0.3.3 resident is running', r1['mode'] == 'resident' and r1['json'].get('version') == '0.3.3')

r2 = call(P34, '2. 0.3.4 launcher vs 0.3.3 resident')
check('2 did not attach across versions (replaced)',
      'not attaching across versions' in r2['log'] and r2['pid'] != r1['pid'])
check('2 old 0.3.3 resident is gone', not alive(r1['pid']))
check('2 now a 0.3.4 resident', r2['mode'] == 'resident' and r2['json'].get('version') == '0.3.4')

r3 = call(P34, '3. 0.3.4 launcher vs 0.3.4 resident')
check('3 same version attaches (same pid, no replace)',
      r3['pid'] == r2['pid'] and 'replacing' not in r3['log'])

r4 = call(P35, '4. 0.3.5 launcher vs 0.3.4 resident')
check('4 did not attach across versions (replaced)',
      'not attaching across versions' in r4['log'] and r4['pid'] != r3['pid'])
check('4 old 0.3.4 resident is gone', not alive(r3['pid']))
check('4 now a 0.3.5 resident', r4['json'].get('version') == '0.3.5')

# 5. unreachable lock holder
zombie = r4['pid']
age_wait = 33
print(f'[drill] 5. deleting resident.json + resident.sock under live pid {zombie}; '
      f'waiting {age_wait}s so it is older than the start window', flush=True)
for f in ('resident.json', 'resident.sock'):
    try:
        os.unlink(f'{RES}/{f}')
    except FileNotFoundError:
        pass
time.sleep(age_wait)
check('5 setup: lock holder alive, unreachable', alive(zombie) and not os.path.exists(f'{RES}/resident.sock'))
r5 = call(P34, '5. 0.3.4 launcher vs unreachable lock holder')
check('5 reclaimed by scan, resident restarted (no private fallback)',
      r5['mode'] == 'resident' and r5['json'].get('version') == '0.3.4' and not alive(zombie))
check('5 no 30s start timeout', r5['secs'] < 25, f'({r5["secs"]:.1f}s)')

r6 = call(P33, '6. (info) 0.3.3 launcher vs 0.3.4 resident')
print(f'[drill] 6. info: old launcher result mode={r6["mode"]} pid={r6["pid"]} '
      f'(0.3.4 resident pid {r5["pid"]}); old launchers have no handshake', flush=True)

print('DRILL', 'PASS' if all(results) else f'FAIL ({results.count(False)} failed)')
sys.exit(0 if all(results) else 1)
