#!/usr/bin/env python3
"""Playwright MCP benchmark: Microsoft's @playwright/mcp (aria
snapshots) driven over MCP stdio, same 3 flows as the Glazyr
benchmark. Tokens = every tool result served to the agent."""
import asyncio, re, time
import tiktoken
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

enc = tiktoken.get_encoding('cl100k_base')
SERVER_CMD = '/opt/node22/bin/node'
SERVER_ARGS = ['/root/bench/node_modules/@playwright/mcp/cli.js', '--headless',
               '--browser', 'chromium']

def text_of(result):
    return ''.join(c.text for c in result.content if hasattr(c, 'text'))

def ref_for(snapshot, *needles, role=None):
    """Find the [ref=eN] on the snapshot line matching role + needles."""
    for ln in snapshot.split('\n'):
        low = ln.lower()
        if role and role not in low:
            continue
        if all(n.lower() in low for n in needles):
            m = re.search(r'\[ref=([a-z0-9]+)\]', ln)
            if m:
                return m.group(1)
    return None

async def main():
    params = StdioServerParameters(command=SERVER_CMD, args=SERVER_ARGS)
    errlog = open('/tmp/pw-mcp-err.log', 'w')
    async with stdio_client(params, errlog=errlog) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = [t.name for t in (await session.list_tools()).tools]
            print('PW TOOLS:', [n for n in names if 'snapshot' in n or 'navigate' in n or 'click' in n or 'type' in n])

            async def call(name, args):
                r = await session.call_tool(name, args)
                t = text_of(r)
                return t, len(enc.encode(t))

            results = {}

            # ---- Flow A ----
            t0 = time.time(); toks = 0
            t, n = await call('browser_navigate', {'url': 'https://en.wikipedia.org/wiki/Chromium_(web_browser)'})
            toks += n
            t, n = await call('browser_snapshot', {})
            toks += n
            snap = t
            ok_license = 'LGPL' in snap
            ref = ref_for(snap, 'Blink', role='link')
            ok_a = False
            if ref:
                t, n = await call('browser_click', {'element': 'Blink link', 'target': ref})
                toks += n
                landed = 'Blink_(browser_engine)' in t
                t, n = await call('browser_snapshot', {})
                toks += n
                snap = t
                ok_a = ok_license and landed
            results['A'] = (toks, round(time.time() - t0, 1), ok_a)

            # ---- Flow B ----
            t0 = time.time(); toks = 0
            t, n = await call('browser_navigate', {'url': 'https://httpbin.org/forms/post'})
            toks += n
            t, n = await call('browser_snapshot', {})
            toks += n
            snap = t
            for needles, role, text in [
                    (('name',), 'textbox', 'Dogfood Tester'),
                    (('mail',), 'textbox', 'tester@example.com')]:
                ref = ref_for(snap, *needles, role=role)
                t, n = await call('browser_type', {'element': ' '.join(needles), 'target': ref, 'text': text})
                toks += n
            # Refs come from the latest snapshot - a careful agent
            # re-snapshots after state changes before clicking on.
            t, n = await call('browser_snapshot', {})
            toks += n
            snap = t
            for needles, role in [(('Medium',), 'radio'), (('Bacon',), 'checkbox')]:
                ref = ref_for(snap, *needles, role=role)
                t, n = await call('browser_click', {'element': ' '.join(needles), 'target': ref})
                toks += n
            t, n = await call('browser_snapshot', {})
            toks += n
            snap = t
            ref = ref_for(snap, 'Submit', role='button')
            t, n = await call('browser_click', {'element': 'Submit order', 'target': ref})
            toks += n
            t, n = await call('browser_snapshot', {})
            toks += n
            print('PW B final snapshot: len', len(t),
                  '| custname:', 'Dogfood Tester' in t,
                  '| bacon:', 'bacon' in t, '| medium:', 'medium' in t)
            print('PW B final tail:', t[-260:].replace('\n', ' | '))
            ok_b = 'Dogfood Tester' in t and 'bacon' in t and 'medium' in t
            results['B'] = (toks, round(time.time() - t0, 1), ok_b)

            # ---- Flow C ----
            t0 = time.time(); toks = 0
            t, n = await call('browser_navigate', {'url': 'https://www.python.org/downloads/'})
            toks += n
            t, n = await call('browser_snapshot', {})
            toks += n
            ok_c = '3.15.2' in t
            results['C'] = (toks, round(time.time() - t0, 1), ok_c)

            for k, (tk, secs, ok) in results.items():
                print(f'PWBENCH {k}: {tk} tokens, {secs}s, ok={ok}')
            print('PW_BENCH_DONE')

asyncio.run(main())
