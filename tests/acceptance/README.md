# Live acceptance harness

Drives a real Glazyr install over MCP, in an isolated namespace, and
judges every trace with the reference checker
(`trace-fixtures/check_fixtures.py`). Root, WSL/Linux, the browser
bundle available at `$GLAZYR_BROWSER_SRC` (default `~/.glazyr/browser`,
copied, never modified).

    tests/acceptance/build_install.sh                 # test home + npm-pack install of this checkout
    RUN=suite tests/acceptance/ns.sh env GLAZYR_PKG_DIR=$GLAZYR_TEST_INSTALL/node_modules/@glazyr/mcp \
        /root/.glazyr/venv/bin/python /root/glazyr-suite/runner.py
    RUN=b  tests/acceptance/ns.sh /root/.glazyr/venv/bin/python tests/acceptance/live_session.py
    RUN=c1 tests/acceptance/ns.sh /root/.glazyr/venv/bin/python tests/acceptance/live_c.py 1
    RUN=c2 tests/acceptance/ns.sh /root/.glazyr/venv/bin/python tests/acceptance/live_c.py 2
    RUN=d  tests/acceptance/ns.sh tests/acceptance/gauntlet_smoke.sh

| File | Role |
|---|---|
| `ns.sh` | pid+net+mount namespace; test home bound over `/root/.glazyr`; `$GLAZYR_EVIDENCE/$RUN/traces` bound over the trace dir, so traces land in evidence directly |
| `build_install.sh` | builds `$GLAZYR_TEST_HOME` (venv + browser copy) and `$GLAZYR_TEST_INSTALL` |
| `live_session.py` | (b) navigate / type / password / checkbox / submit; secret-leak and chain checks |
| `live_c.py`, `worker.py` | (c) producer kill / renderer wedge, client kill mid-click (resident and private tree kill), resume from a dead file |
| `gauntlet_smoke.sh` | (d) three separate mcporter 0.17.1 processes against the Gauntlet fixture (needs Node >= 20 at `/opt/node22`) |
| `refcheck.py`, `dump_trace.py` | reference-checker verdicts; one-line-per-event dump |
| `fixtures/` | login / done pages (`/slow` is served by `live_c.py` with a 25 s delay) |

Why the namespace: the bridge's `_kill_shells` and the suite's `preclean`
kill every matching process on the machine, CDP is fixed at 9224, and
`stdio_client`'s minimal environment drops `GLAZYR_HOME` - outside a
namespace any of these reaches the daily-driver install.

Defaults: `GLAZYR_TEST_HOME=/root/glazyr-034-home`,
`GLAZYR_TEST_INSTALL=/root/glazyr-034-install`,
`GLAZYR_TEST_PKG=$GLAZYR_TEST_INSTALL/node_modules/@glazyr/mcp`,
`GLAZYR_EVIDENCE=/root/glazyr-acceptance-evidence`.
