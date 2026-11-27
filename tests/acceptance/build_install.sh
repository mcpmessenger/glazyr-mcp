#!/bin/bash
# Build the branch-only test environment (never touches ~/.glazyr):
#   $GLAZYR_TEST_HOME: venv (bridge deps + rfc8785) and a COPY of the browser bundle
#   $GLAZYR_TEST_INSTALL: npm install of `npm pack` of this checkout
set -eu
REPO=$(cd "$(dirname "$0")/../.." && pwd)
H=${GLAZYR_TEST_HOME:-/root/glazyr-034-home}
I=${GLAZYR_TEST_INSTALL:-/root/glazyr-034-install}
BROWSER_SRC=${GLAZYR_BROWSER_SRC:-/root/.glazyr/browser}
mkdir -p "$H" "$I"
[ -x "$H/browser/headless_shell" ] || cp -a "$BROWSER_SRC" "$H/browser"
if [ ! -x "$H/venv/bin/python" ]; then
  python3 -m venv "$H/venv"
  "$H/venv/bin/pip" install --quiet "mcp==1.30.0" "protobuf==7.36.2" websocket-client "tiktoken==0.14.0" rfc8785
  "$H/venv/bin/python" -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"   # warm the cache (no network in the ns)
fi
cd "$REPO"
find . -name __pycache__ -type d -prune -exec rm -rf {} +
rm -f glazyr-mcp-*.tgz && npm pack --silent >/dev/null
TGZ=$(ls "$REPO"/glazyr-mcp-*.tgz)
cd "$I" && [ -f package.json ] || echo '{"name":"glazyr-test-install","private":true}' > package.json
rm -rf node_modules package-lock.json && npm install --silent --no-audit --no-fund "$TGZ"
rm -f "$TGZ"
echo "installed $(node -e "console.log(require('./node_modules/@glazyr/mcp/package.json').version)") at $I"
