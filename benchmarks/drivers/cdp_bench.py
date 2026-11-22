#!/usr/bin/env python3
"""Raw-CDP benchmark: what an agent gets wiring an LLM straight to
Chrome DevTools - Accessibility.getFullAXTree dumps as the state
representation, JS queries + Input events for actions. Same 3 flows
as the Glazyr benchmark. Tokens = every AX dump served to the agent."""
import json, subprocess, time, urllib.request
import websocket, tiktoken

enc = tiktoken.get_encoding('cl100k_base')
PORT = 9333
SHELL = '/root/chromium/src/out/Default/headless_shell'
UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36')

proc = subprocess.Popen(
    [SHELL, '--no-sandbox', '--window-size=1280,1024', f'--user-agent={UA}',
     f'--remote-debugging-port={PORT}', 'about:blank'],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def targets():
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/json', timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None

for _ in range(40):
    if targets():
        break
    time.sleep(0.3)
page = next(t for t in targets() if t.get('type') == 'page')
ws = websocket.create_connection(page['webSocketDebuggerUrl'], timeout=30)
_id = 0

def cdp(method, params=None):
    global _id
    _id += 1
    mid = _id
    ws.send(json.dumps({'id': mid, 'method': method, 'params': params or {}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get('id') == mid:
            return msg.get('result', {})

def wait_ready(timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = cdp('Runtime.evaluate', {'expression': 'document.readyState',
                                     'returnByValue': True})
        if r.get('result', {}).get('value') == 'complete':
            time.sleep(0.8)
            return
        time.sleep(0.3)

def dump_ax():
    """Naive indented serialization of the full AX tree - the dump a
    straightforward CDP integration hands the model."""
    r = cdp('Accessibility.getFullAXTree')
    nodes = r.get('nodes', [])
    by_id = {n['nodeId']: n for n in nodes}
    kids = {}
    roots = []
    for n in nodes:
        for cid in n.get('childIds', []):
            kids[cid] = n['nodeId']
    for n in nodes:
        if n['nodeId'] not in kids:
            roots.append(n['nodeId'])
    lines = []
    def val(v):
        return v.get('value', '') if isinstance(v, dict) else ''
    def rec(nid, depth):
        n = by_id[nid]
        role = val(n.get('role', {}))
        name = val(n.get('name', {}))
        v = val(n.get('value', {}))
        line = '  ' * depth + f'- {role} "{name}"'
        if v:
            line += f' value="{v}"'
        props = {p['name']: val(p.get('value', {})) for p in n.get('properties', [])}
        if props.get('checked'):
            line += ' [checked]'
        lines.append(line)
        for cid in n.get('childIds', []):
            if cid in by_id:
                rec(cid, depth + 1)
    for rid in roots:
        rec(rid, 0)
    return '\n'.join(lines)

def js(expr):
    r = cdp('Runtime.evaluate', {'expression': expr, 'returnByValue': True})
    return r.get('result', {}).get('value')

def click_xy(x, y):
    cdp('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y})
    cdp('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})
    cdp('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})

def center_of(selector_expr):
    return js(f'''(() => {{ const el = {selector_expr}; if (!el) return null;
        el.scrollIntoView({{block: 'center'}});
        const r = el.getBoundingClientRect();
        return [r.x + r.width / 2, r.y + r.height / 2]; }})()''')

results = {}

# ---- Flow A: Wikipedia Chromium -> Blink ----
t0 = time.time(); toks = 0
cdp('Page.enable'); cdp('Accessibility.enable')
cdp('Page.navigate', {'url': 'https://en.wikipedia.org/wiki/Chromium_(web_browser)'})
wait_ready()
d = dump_ax(); toks += len(enc.encode(d))
ok_license = 'LGPL' in d
xy = center_of('''[...document.querySelectorAll('a[href$="Blink_(browser_engine)"]')].find(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })''')
landed = False
if xy:
    click_xy(*xy)
    deadline = time.time() + 12
    while time.time() < deadline:
        if 'Blink_(browser_engine)' in (js('location.href') or ''):
            landed = True
            break
        time.sleep(0.3)
    wait_ready()
d = dump_ax(); toks += len(enc.encode(d))
ok_a = ok_license and landed
results['A'] = (toks, round(time.time() - t0, 1), ok_a)

# ---- Flow B: httpbin form ----
t0 = time.time(); toks = 0
cdp('Page.navigate', {'url': 'https://httpbin.org/forms/post'})
wait_ready()
d = dump_ax(); toks += len(enc.encode(d))
for name, text in [('custname', 'Dogfood Tester'), ('custemail', 'tester@example.com')]:
    xy = center_of(f'''document.querySelector('input[name="{name}"]')''')
    click_xy(*xy)
    cdp('Input.insertText', {'text': text})
for sel in ['''document.querySelector('input[name="size"][value="medium"]')''',
            '''document.querySelector('input[name="topping"][value="bacon"]')''']:
    xy = center_of(sel)
    if xy is None:
        print('CDP selector miss:', js('''JSON.stringify([...document.querySelectorAll('input')].map(i => i.name + '=' + i.value + '/' + i.type))'''))
    click_xy(*xy)
xy = center_of('''document.querySelector('form button') || document.querySelector('input[type="submit"]') || document.querySelector('button')''')
if xy is None:
    print('CDP submit miss:', js('''document.querySelectorAll('button').length'''))
click_xy(*xy)
wait_ready()
d = dump_ax(); toks += len(enc.encode(d))
ok_b = 'Dogfood Tester' in d and 'bacon' in d and 'medium' in d
results['B'] = (toks, round(time.time() - t0, 1), ok_b)

# ---- Flow C: python.org downloads ----
t0 = time.time(); toks = 0
cdp('Page.navigate', {'url': 'https://www.python.org/downloads/'})
wait_ready()
d = dump_ax(); toks += len(enc.encode(d))
ok_c = '3.15.2' in d
results['C'] = (toks, round(time.time() - t0, 1), ok_c)

for k, (tk, secs, ok) in results.items():
    print(f'CDPBENCH {k}: {tk} tokens, {secs}s, ok={ok}')
proc.terminate()
print('CDP_BENCH_DONE')
