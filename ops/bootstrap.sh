#!/usr/bin/env bash
# bootstrap.sh: link the modules into the core and check the tools (plan 23 W14, GUIDE Phase 2).
#
#   ops/bootstrap.sh [--check-only]
#
# AzerothCore only looks for modules in <core>/modules, and has no variable for looking anywhere else.
# Git will not nest one submodule inside another's tree. So the modules live at src/modules/ and are
# LINKED into the core here. CMake's GLOB and the runtime SQL scan both follow symlinks.
#
# Nothing here needs sudo, touches a database, or reaches the network.
set -euo pipefail
HERE=$(cd "$(dirname "$(realpath "$0")")" && pwd)
REPO=$(cd "$HERE/.." && pwd)

CHECK_ONLY=0
case "${1:-}" in
  --check-only) CHECK_ONLY=1 ;;
  -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
  "") ;;
  *) echo "unknown argument: $1 (see --help)"; exit 2 ;;
esac

say() { echo "[$(date +%H:%M:%S)] $*"; }
miss=0

# --- the tools this project needs -------------------------------------------
say "checking tools"
need() {
  if command -v "$1" >/dev/null 2>&1; then
    printf '    ok      %-14s %s\n' "$1" "$(command -v "$1")"
  else
    printf '    MISSING %-14s %s\n' "$1" "$2"; miss=$((miss + 1))
  fi
}
need git        "apt install git"
need cmake      "apt install cmake"
need make       "apt install make"
need gcc        "apt install build-essential"
need mysql      "apt install mysql-client (the server too, if it runs here)"
need mysqldump  "apt install mysql-client — backups need it"
need python3    "apt install python3 (3.11+, for tomllib)"
need nc         "apt install netcat-openbsd — the GM console helper uses nc -q"
need openssl    "apt install openssl — the setup generates secrets with it"

py=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 0.0)
case "$py" in
  3.1[1-9]|3.[2-9]*) printf '    ok      %-14s %s\n' "python 3.11+" "$py" ;;
  *) printf '    MISSING %-14s found %s; fleet.toml needs tomllib\n' "python 3.11+" "$py"; miss=$((miss + 1)) ;;
esac

# --- the layout --------------------------------------------------------------
CORE="$REPO/src/azerothcore-wotlk"
MODS="$REPO/src/modules"
if [ ! -d "$CORE" ]; then
  echo
  echo "no core checkout at src/azerothcore-wotlk."
  echo "This repository expects the superproject layout: the core and the modules as submodules under"
  echo "src/. If you cloned without --recursive, run:"
  echo
  echo "    git -C \"$REPO\" submodule update --init --recursive"
  echo
  echo "If your core lives elsewhere (an older checkout, say), point CORE_DIR at it in site/site.env"
  echo "and link the modules in by hand — that is all this script does."
  exit 1
fi

say "linking modules into the core"
mkdir -p "$CORE/modules"
linked=0
for m in "$MODS"/*/; do
  [ -d "$m" ] || continue
  n=$(basename "$m")
  target="$CORE/modules/$n"
  if [ "$CHECK_ONLY" = "1" ]; then
    [ -e "$target" ] && printf '    ok      %s\n' "$n" || { printf '    MISSING link %s\n' "$n"; miss=$((miss + 1)); }
  else
    ln -sfn "../../modules/$n" "$target"
    printf '    linked  %s\n' "$n"
  fi
  linked=$((linked + 1))
done
[ "$linked" = "0" ] && { echo "    no modules found under src/modules/"; miss=$((miss + 1)); }

# mod-dashboard compiles against headers from mod-ollama-chat and mod-playerbots, so all three have to
# be present in the same build. Saying so here beats a link error forty minutes into the build.
for needed in mod-playerbots mod-ollama-chat mod-dashboard; do
  [ -e "$CORE/modules/$needed" ] || { echo "    MISSING module $needed (mod-dashboard needs the other two to compile)"; miss=$((miss + 1)); }
done

echo
if [ "$miss" -gt 0 ]; then
  echo "$miss thing(s) missing above. Install what is listed (apt needs sudo: hand the operator one"
  echo "command at a time, GUIDE gate G3), then run this again."
  exit 1
fi

say "ready"
echo
echo "Next: fill in site/site.env from gate G1 — WOW_ROOT, SERVER_PREFIX, DATA_DIR, LOGS_DIR,"
echo "BACKUP_DIR, WOW_USER, DB_*, REALM_NAME, REALM_ADDRESS, ERA, REALM_FIRST_DAY, MAP_THREADS,"
echo "BOT_MIN/BOT_MAX (50/100 for a first boot) and BOT_GUILDS (20)."
echo "Then: ops/build.sh"
