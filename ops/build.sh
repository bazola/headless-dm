#!/usr/bin/env bash
# build.sh: configure and build the server (plan 23 W14, GUIDE Phase 4).
#
#   ops/build.sh [--jobs N] [--fresh] [--configure-only] [--foreground]
#
# Runs detached by default and prints how to watch it: a full build is 30-90 minutes, and a shell that
# closes should not take it with it. Never uses sudo -- it installs into SERVER_PREFIX, which the setup
# made writable by WOW_USER.
#
#   -DMODULES=static is required: mod-dashboard compiles against headers from mod-ollama-chat
#   (cpp-httplib, nlohmann/json) and from mod-playerbots, so they must be in the same binary.
#   -DTOOLS_BUILD=all builds the extractors Phase 6 needs. Skipping it costs you a second build.
set -euo pipefail
HERE=$(cd "$(dirname "$(realpath "$0")")" && pwd)
. "$HERE/env.sh"

JOBS=$(nproc)
FRESH=0
CONFIGURE_ONLY=0
FOREGROUND=0
while [ $# -gt 0 ]; do
  case "$1" in
    --jobs) JOBS=${2:?--jobs needs a number}; shift ;;
    --fresh) FRESH=1 ;;
    --configure-only) CONFIGURE_ONLY=1 ;;
    --foreground) FOREGROUND=1 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1 (see --help)"; exit 2 ;;
  esac
  shift
done

: "${SERVER_PREFIX:?SERVER_PREFIX is not set in site/site.env}"
CORE=${CORE_DIR:-$REPO/src/azerothcore-wotlk}
[ -d "$CORE" ] || { echo "no core checkout at $CORE — set CORE_DIR, or run ops/bootstrap.sh first"; exit 1; }
[ -d "$CORE/modules" ] || { echo "$CORE/modules is missing — run ops/bootstrap.sh to link the modules in"; exit 1; }

say() { echo "[$(date +%H:%M:%S)] $*"; }
BUILD="$CORE/build"
[ "$FRESH" = "1" ] && { say "removing $BUILD"; rm -rf "$BUILD"; }
mkdir -p "$BUILD" "$LOGS_DIR/setup"

say "modules that will be built in:"
ls -1 "$CORE/modules" 2>/dev/null | sed 's/^/    /'

say "configuring (prefix $SERVER_PREFIX)"
cd "$BUILD"
cmake ../ \
  -DCMAKE_INSTALL_PREFIX="$SERVER_PREFIX" \
  -DCMAKE_BUILD_TYPE=Release \
  -DWITH_WARNINGS=0 \
  -DSCRIPTS=static \
  -DMODULES=static \
  -DTOOLS_BUILD=all

[ "$CONFIGURE_ONLY" = "1" ] && { say "configured; stopping before the build as asked"; exit 0; }

LOG="$LOGS_DIR/setup/build-$(date +%Y%m%d-%H%M%S).log"
if [ "$FOREGROUND" = "1" ]; then
  say "building with $JOBS jobs, in the foreground"
  nice -n 10 make -j"$JOBS" install 2>&1 | tee "$LOG"
else
  # < /dev/null matters: without it the job can hang waiting on input it will never get.
  nohup nice -n 10 make -j"$JOBS" install > "$LOG" 2>&1 < /dev/null &
  pid=$!
  echo "$pid" > "$LOGS_DIR/setup/build.pid"
  say "building with $JOBS jobs, detached (pid $pid)"
  say "watch:   tail -f $LOG"
  say "check:   kill -0 $pid && echo running || echo finished"
  say "when it finishes, the binaries are in $SERVER_PREFIX/bin"
fi
