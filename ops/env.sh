# ops/env.sh — the environment every command in the guide starts with (plan 23 W1).
#
#   . ops/env.sh && <command>
#
# Sources site/site.env and site/secrets.env, and defines the two helpers that keep passwords off the
# command line. Meant to be sourced, not run: it sets variables in your shell.
#
# SITE_DIR can point somewhere else (a private site repo holding this machine's answers), which is the
# one knob that lets the public tree stay generic.

REPO=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)
SITE_DIR=${SITE_DIR:-$REPO/site}

if [ ! -f "$SITE_DIR/site.env" ]; then
  echo "no $SITE_DIR/site.env — copy site.example/site.env to site/ and fill it in (GUIDE Phase 2)" >&2
else
  # set -a exports everything these files define, so python3 services/common/site.py sees the same values.
  set -a
  . "$SITE_DIR/site.env"
  [ -f "$SITE_DIR/secrets.env" ] && . "$SITE_DIR/secrets.env"
  set +a
fi

export REPO SITE_DIR
LOGS_DIR=${LOGS_DIR:-$REPO/logs}
export LOGS_DIR

# The database, always through here. MYSQL_PWD keeps the password out of the process list, and utf8mb4 is
# not optional: the mysql client otherwise defaults to latin1 and double-encodes every accent it stores.
db() {
  MYSQL_PWD="$DB_PASS" mysql --default-character-set=utf8mb4 \
    -h"${DB_HOST:-127.0.0.1}" -P"${DB_PORT:-3306}" -u"${DB_USER:-acore}" "$@"
}

# The GM console. It serves one session at a time, so never run two of these at once; a client that did
# not close blocks every later one (find it with `pgrep -a nc`, kill it by PID).
ra() {
  "$REPO/ops/scripts/ra.sh" "$@"
}
