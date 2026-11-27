#!/usr/bin/env python3
"""MCP client worker for acceptance (c): runs JSON steps over stdio
against the launcher at $GLAZYR_TEST_PKG, printing markers the orchestrator times
kills on. argv[1] = JSON list of steps; env GLAZYR_RESIDENT passes through."""
import asyncio, json, os, re, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PKG = os.environ.get('GLAZYR_TEST_PKG', '/root/glazyr-034-install/node_modules/@glazyr/mcp')


def text(r):
    return '\n'.join(c.text for c in r.content if getattr(c, 'type', '') == 'text')


async def main():
    steps = json.loads(sys.argv[1])
    env = {k: v for k, v in os.environ.items()
           if k in ('HOME', 'PATH', 'GLAZYR_RESIDENT', 'GLAZYR_TEST_PKG', 'USER', 'LOGNAME', 'SHELL', 'TERM')}
    params = StdioServerParameters(command='node', args=[f'{PKG}/launcher.js'], env=env)
    ids = {}
    async with stdio_client(params, errlog=sys.stderr) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for i, st in enumerate(steps):
                if st.get('pause'):
                    print(f'@@PAUSE {i} {st["pause"]}', flush=True)
                    await asyncio.sleep(st['pause'])
                    continue
                args = dict(st.get('args', {}))
                for k, v in list(args.items()):
                    if isinstance(v, str) and v.startswith('$'):
                        args[k] = ids[v[1:]]
                print(f'@@STEP {i} {st["tool"]} {json.dumps(args)}', flush=True)
                res = await s.call_tool(st['tool'], args)
                t = text(res)
                print(('@@ISERROR\n' if res.isError else '') + t, flush=True)
                for name, pat in (st.get('grab') or {}).items():
                    m = re.search(pat, t)
                    if m:
                        ids[name] = int(m.group(1))
                print(f'@@DONE {i}', flush=True)


asyncio.run(main())
