#!/bin/bash
# Acceptance (d): Gauntlet smoke against the 0.3.4 BRANCH install only.
# Same shape as the 0.3.3 post-publish smoke: three separate mcporter
# 0.17.1 processes (navigate full / peek / status) through the launcher's
# resident. Run inside ns.sh:
#   RUN=d tests/acceptance/ns.sh tests/acceptance/gauntlet_smoke.sh
set -u
# Same Node the 0.3.3 smoke used: mcporter 0.17.1 needs import attributes (Node >= 20).
export PATH=/opt/node22/bin:$PATH
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
W=${EVID_RUN:-/tmp/glazyr-gauntlet-smoke}; mkdir -p "$W"
PKG=${GLAZYR_TEST_PKG:-/root/glazyr-034-install/node_modules/@glazyr/mcp}
GAUNTLET=${GLAZYR_GAUNTLET_DIR:-/root/glazyr-gauntlet}
MCPORTER=$(ls -d /root/.npm/_npx/*/node_modules/mcporter | head -1)
OUT=$W/gauntlet_smoke.txt
: > $OUT
exec > >(tee -a $OUT) 2>&1
echo "=== 0. target ==="
echo "package: $PKG version $(node -e "console.log(require('$PKG/package.json').version)")"
PKGV=$(node -e "console.log(require('$PKG/package.json').version)")
echo "node: $(node --version)"
echo "mcporter: $(node -e "console.log(require('$MCPORTER/package.json').version)")"
cat > $W/mcporter.json <<EOF
{"mcpServers": {"glazyr": {"command": "node", "args": ["$PKG/launcher.js"]}}}
EOF
echo "=== 1. fixture ==="
cd $GAUNTLET && python3 -m http.server 8943 --bind 127.0.0.1 >/dev/null 2>&1 &
SRV=$!
sleep 1
curl -s -m 5 http://127.0.0.1:8943/gauntlet.html | grep -o 'GAUNTLET-HEADING-[0-9]*' | head -1
MC="node $MCPORTER/dist/cli.js --config $W/mcporter.json"
[ -f "$MCPORTER/dist/cli.js" ] || MC="$MCPORTER/../.bin/mcporter --config $W/mcporter.json"
echo "=== 2. call 1: navigate full (separate process) ==="
timeout 150 $MC call glazyr.glazyr_navigate url=http://127.0.0.1:8943/gauntlet.html full=true --output json > $W/g1.json
head -c 600 $W/g1.json; echo; tail -c 200 $W/g1.json; echo
P1=$(python3 -c "import json;print(json.load(open('/root/.glazyr/resident.json'))['pid'])")
V1=$(python3 -c "import json;print(json.load(open('/root/.glazyr/resident.json'))['version'])")
echo "resident pid after call1: $P1 version: $V1"
echo "=== 3. call 2: peek (separate process) ==="
timeout 150 $MC call glazyr.glazyr_peek --output json > $W/g2.json; cat $W/g2.json; echo
P2=$(python3 -c "import json;print(json.load(open('/root/.glazyr/resident.json'))['pid'])")
echo "=== 4. call 3: status (separate process) ==="
timeout 150 $MC call glazyr.glazyr_status --output json > $W/g3.json; cat $W/g3.json; echo
P3=$(python3 -c "import json;print(json.load(open('/root/.glazyr/resident.json'))['pid'])")
echo "=== 5. call 4: glazyr_trace summary (separate process) ==="
timeout 150 $MC call glazyr.glazyr_trace mode=summary session='*' --output json > $W/g4.json; cat $W/g4.json; echo
echo "=== 6. assertions ==="
a() { if eval "$2"; then echo "ASSERT $1: PASS"; else echo "ASSERT $1: FAIL"; fi; }
a "call1 heading GAUNTLET-HEADING-2210" "grep -q GAUNTLET-HEADING-2210 $W/g1.json"
a "call1 below-fold FOLD-TOKEN-3304" "grep -q FOLD-TOKEN-3304 $W/g1.json"
a "call2 unchanged stub (state kept across processes)" "grep -q 'unchanged seq=' $W/g2.json"
a "no about:blank in any result" "! grep -q about:blank $W/g1.json $W/g2.json $W/g3.json"
a "status: resident mode" "grep -q 'bridge_mode: resident' $W/g3.json"
a "status: producer live" "grep -q 'producer: live' $W/g3.json"
a "one resident across calls ($P1/$P2/$P3)" "[ '$P1' = '$P2' ] && [ '$P2' = '$P3' ]"
a "resident version = package version ($PKGV)" "[ '$V1' = '$PKGV' ]"
a "trace summary chain ok" "grep -q 'chain: ok' $W/g4.json"
echo "=== 7. trace file (reference checker) ==="
F=$(ls -t /root/.glazyr/traces/*.trace.jsonl | head -1)
/root/.glazyr/venv/bin/python "$HERE/refcheck.py" "$F" && echo "ASSERT trace chain ok (reference checker): PASS" || echo "ASSERT trace chain ok (reference checker): FAIL"
kill $SRV 2>/dev/null
echo "SMOKE DONE $(date -u +%FT%TZ)"
