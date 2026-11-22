#!/usr/bin/env python3
"""Glazyr v0.2.2 benchmark: the three dogfood flows, scripted, with
tokens_served_total snapshots between flows. Compare against the
v0.2.1 dogfood actuals (Claude: T1 29,652 / T2 ~1,460 / T3 18,451;
Antigravity: T1 ~27,804 / T2 ~1,117 / T3 ~39,050)."""
import asyncio, re
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = '/root/glazyr-bridge/glazyr_mcp.py'

def text_of(result):
    return ''.join(c.text for c in result.content if hasattr(c, 'text'))

async def total(session):
    t = text_of(await session.call_tool('glazyr_status', {}))
    m = re.search(r'tokens_served_total: (\d+)', t)
    return int(m.group(1)) if m else -1

async def main():
    params = StdioServerParameters(command='/root/glazyr-bridge/venv/bin/python',
                                   args=[SERVER])
    errlog = open('/tmp/server-err-bench.log', 'w')
    async with stdio_client(params, errlog=errlog) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            marks = []

            # ---- Flow A: Wikipedia Chromium -> Blink ----
            t0 = await total(session)
            await session.call_tool('glazyr_navigate',
                {'url': 'https://en.wikipedia.org/wiki/Chromium_(web_browser)'})
            r = await session.call_tool('glazyr_find', {'query': 'Blink'})
            t = text_of(r)
            m = re.search(r"\[(\d+)\][^\n]*Blink_\(browser_engine\)", t)
            blink_id = int(m.group(1))
            r = await session.call_tool('glazyr_click', {'node_id': blink_id})
            t = text_of(r)
            ok_a = 'Blink' in t
            t1 = await total(session)
            marks.append(('A wiki Chromium->Blink', t1 - t0, ok_a))

            # ---- Flow B: httpbin form ----
            t0 = await total(session)
            await session.call_tool('glazyr_navigate',
                {'url': 'https://httpbin.org/forms/post'})
            r = await session.call_tool('glazyr_find', {'query': 'custname'})
            t = text_of(r)
            cust = int(re.search(r"\[(\d+)\] input INPUT_TEXT name='custname'", t).group(1))
            await session.call_tool('glazyr_type', {'node_id': cust, 'text': 'Dogfood Tester'})
            r = await session.call_tool('glazyr_find', {'query': 'custemail'})
            t = text_of(r)
            em = int(re.search(r"\[(\d+)\] input INPUT_EMAIL name='custemail'", t).group(1))
            await session.call_tool('glazyr_type', {'node_id': em, 'text': 'tester@example.com'})
            r = await session.call_tool('glazyr_peek', {'force': True})
            t = text_of(r)
            med = int(re.search(r"\[(\d+)\] input INPUT_RADIO name='size' label='Medium'", t).group(1))
            bacon = int(re.search(r"\[(\d+)\] input INPUT_CHECKBOX name='topping' label='Bacon'", t).group(1))
            await session.call_tool('glazyr_click', {'node_id': med})
            await session.call_tool('glazyr_click', {'node_id': bacon})
            r = await session.call_tool('glazyr_peek_raw', {})
            t = text_of(r)
            submit_id = None
            lines = t.split('\n')
            for i, ln in enumerate(lines):
                if 'Submit' in ln:
                    for j in range(i, max(-1, i - 4), -1):
                        mm = re.search(r"\[(\d+)\]", lines[j])
                        if mm:
                            submit_id = int(mm.group(1)); break
                    if submit_id: break
            r = await session.call_tool('glazyr_click', {'node_id': submit_id})
            t = text_of(r)
            ok_b = 'Dogfood Tester' in t and 'bacon' in t
            t1 = await total(session)
            marks.append(('B httpbin form', t1 - t0, ok_b))

            # ---- Flow C: python.org downloads ----
            t0 = await total(session)
            await session.call_tool('glazyr_navigate',
                {'url': 'https://www.python.org/downloads/'})
            r = await session.call_tool('glazyr_find', {'query': '3.15.2'})
            t = text_of(r)
            ok_c = 'match(es)' in t and '3.15.2' in t
            t1 = await total(session)
            marks.append(('C python.org find 3.15.2', t1 - t0, ok_c))

            for name, toks, ok in marks:
                print(f'BENCH {name}: {toks} tokens, ok={ok}')
            print('BENCH_DONE')

asyncio.run(main())
