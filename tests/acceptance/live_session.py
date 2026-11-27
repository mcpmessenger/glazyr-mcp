#!/usr/bin/env python3
"""Acceptance (b): scripted live session against the BRANCH install over
real MCP stdio (launcher -> resident). Run inside ns.sh:
    RUN=b tests/acceptance/ns.sh /root/.glazyr/venv/bin/python tests/acceptance/live_session.py
"""
import asyncio, json, os, re, subprocess, sys, time, glob
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.environ.get('GLAZYR_TEST_PKG', '/root/glazyr-034-install/node_modules/@glazyr/mcp')
FIX = os.path.join(HERE, 'fixtures')
BASE = 'http://127.0.0.1:8950'
SECRET = 'hunter2-SECRET-7731'
TRACES = '/root/.glazyr/traces'

def text(r):
    return '\n'.join(c.text for c in r.content if getattr(c, 'type', '') == 'text')

async def main():
    srv = subprocess.Popen([sys.executable, '-m', 'http.server', '8950', '--bind', '127.0.0.1'],
                           cwd=FIX, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    served = []
    errors = []
    params = StdioServerParameters(command='node', args=[f'{PKG}/launcher.js'])
    async with stdio_client(params, errlog=sys.stderr) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            async def call(tool, **args):
                res = await s.call_tool(tool, args)
                t = text(res)
                if res.isError:
                    errors.append((tool, t[:300]))
                served.append(t)
                print(f'--- {tool} {args if "text" not in args or tool == "glazyr_trace" else {**args, "text": "<..>"}}')
                print(t if len(t) < 1600 else t[:1600] + '\n[...]')
                return t
            await call('glazyr_trace', mode='note', label='goal',
                       text='Sign in to the fixture site as ada, remember me, land on the done page.')
            await call('glazyr_navigate', url=f'{BASE}/login.html')
            pk = await call('glazyr_peek')
            uid = int(re.search(r"\[(\d+)\] input INPUT_TEXT name='user'", pk).group(1))
            pid = int(re.search(r"\[(\d+)\] input INPUT_PASSWORD name='password'", pk).group(1))
            cid = int(re.search(r"\[(\d+)\] input INPUT_CHECKBOX name='remember'", pk).group(1))
            await call('glazyr_type', node_id=uid, text='ada')
            await call('glazyr_type', node_id=pid, text=SECRET)
            await call('glazyr_click', node_id=cid)
            pk2 = await call('glazyr_peek')
            await call('glazyr_find', query='hunter2')
            raw = await call('glazyr_peek_raw')
            sid = int(re.search(r"\[(\d+)\] input BUTTON_SUBMIT[^\n]*\n\s+text 'Sign in now'", raw).group(1))
            await call('glazyr_click', node_id=sid)
            await call('glazyr_peek', force=True)
            await call('glazyr_trace', mode='tail')
            await call('glazyr_trace', mode='summary')
            await call('glazyr_trace', mode='since', since=4)
            await call('glazyr_trace', mode='url', url=f'{BASE}/login.html')
            await call('glazyr_trace', mode='verify')
    srv.kill()
    time.sleep(1.5)   # let the resident write session_end
    print('\n===== CHECKS =====')
    print('tool errors:', errors)
    leaked = [i for i, t in enumerate(served) if SECRET in t]
    print('secret in any tool response:', leaked or 'none')
    files = sorted(glob.glob(f'{TRACES}/*.trace.jsonl'), key=os.path.getmtime)
    f = files[-1]
    raw_file = open(f, encoding='utf-8').read()
    print('trace file:', f)
    print('secret in trace file:', SECRET in raw_file, '| "hunter2" in trace:', 'hunter2' in raw_file)
    cf = os.path.join(HERE, '..', '..', 'trace-fixtures', 'check_fixtures.py')
    src = open(cf).read().split('EXPECT = {')[0]
    ns = {'__file__': cf}
    exec(src, ns)
    v = ns['check'](f)
    ok = (v['chain'] == 'ok' and not v['interrupted'] and not errors and not leaked
          and SECRET not in raw_file)
    print('RESULT:', 'PASS' if ok else 'FAIL')
    print('REFERENCE CHECKER: chain=%s verified=%s interrupted=%s violations=%s' %
          (v['chain'], v['verified'], v['interrupted'], v.get('violations')))
    for e in v['events']:
        k = e['kind']
        extra = {x: y for x, y in e.items() if x not in ('v', 'ts', 'session', 'prev_hash', 'capabilities', 'hashes', 'counts', 'n', 'kind', 'emitter', 'stack')}
        print(f"  n={e['n']:>2} {k:<15} {json.dumps(extra, ensure_ascii=False)[:190]}")

asyncio.run(main())
