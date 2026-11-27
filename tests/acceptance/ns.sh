#!/bin/bash
# Run "$@" in an isolated pid+net+mount namespace:
#   - own /proc: no other Glazyr/browser process is visible or killable
#     (the suite's preclean and the bridge's _kill_shells scan /proc);
#   - own loopback: CDP 9224 and fixture ports are private;
#   - $GLAZYR_TEST_HOME bound over /root/.glazyr, so nothing inside can
#     reach the real (daily-driver) home;
#   - $GLAZYR_EVIDENCE/$RUN/traces bound over /root/.glazyr/traces: trace
#     files are written straight into the run's evidence directory and
#     survive teardown (nothing to copy, nothing to wipe between runs).
# Usage: RUN=<label> tests/acceptance/ns.sh <command> [args...]
# Inside, $EVID_RUN is the run's evidence directory (for logs).
set -u
HOME_DIR=${GLAZYR_TEST_HOME:-/root/glazyr-034-home}
EVID=${GLAZYR_EVIDENCE:-/root/glazyr-acceptance-evidence}
RUN=${RUN:-run-$(date -u +%Y%m%dT%H%M%SZ)}
export EVID_RUN="$EVID/$RUN"
mkdir -p "$EVID_RUN/traces" "$HOME_DIR/traces"
echo "[ns] run=$RUN evidence=$EVID_RUN" >&2
exec unshare --pid --net --mount --fork --mount-proc bash -c '
  ip link set lo up &&
  mount --bind "$0" /root/.glazyr &&
  mount --bind "$EVID_RUN/traces" /root/.glazyr/traces || { echo "[ns] setup failed" >&2; exit 97; }
  exec "$@"' "$HOME_DIR" "$@"
