#!/usr/bin/env bash
# extract-data.sh: the client data the server needs (plan 23 W14, GUIDE Phase 6.1 and 6.2).
#
#   ops/extract-data.sh [--view-only] [--threads N] [--foreground]
#
# Two steps. First a folder of links to exactly the STOCK archives, because the extractors expect exact
# names (Data/common.MPQ, Data/enUS/locale-enUS.MPQ) and many client copies are lowercase. Then the four
# extractors, which take about ten minutes.
#
# Nothing here copies client files anywhere. The links point at the operator's own client and the output
# is derived data; neither ever goes near git.
#
# Four archives are deliberately NEVER linked, even when the client has them:
#   patch-6        terrain -- spawns are placed on stock terrain
#   patch-k        starting outfits
#   patch-w        liquids
#   patch-enus-a   character appearance rows
# An HD repack is fine: its patches simply are not extracted.
set -euo pipefail
HERE=$(cd "$(dirname "$(realpath "$0")")" && pwd)
. "$HERE/env.sh"

VIEW_ONLY=0
THREADS=$(nproc)
FOREGROUND=0
while [ $# -gt 0 ]; do
  case "$1" in
    --view-only) VIEW_ONLY=1 ;;
    --threads) THREADS=${2:?--threads needs a number}; shift ;;
    --foreground) FOREGROUND=1 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1 (see --help)"; exit 2 ;;
  esac
  shift
done

: "${CLIENT_DIR:?CLIENT_DIR is not set in site/site.env (gate G2: the operator puts their client in place)}"
[ -d "$CLIENT_DIR" ] || { echo "CLIENT_DIR does not exist: $CLIENT_DIR"; exit 1; }
: "${DATA_DIR:?DATA_DIR is not set in site/site.env}"
BIN="${SERVER_PREFIX:?}/bin"
VIEW="${WOW_ROOT:-$REPO}/extract/client"
say() { echo "[$(date +%H:%M:%S)] $*"; }

# --- 6.1 the view of the stock archives -------------------------------------
mkdir -p "$VIEW/Data/enUS"
missing=0
link() {  # link <subdir> <depth> <name...>
  local sub=$1 depth=$2; shift 2
  for f in "$@"; do
    local want="$f" src
    src=$(find "$CLIENT_DIR" -maxdepth "$depth" -ipath "*/data${sub}/$want" 2>/dev/null | head -1)
    if [ -n "$src" ]; then
      ln -sfn "$src" "$VIEW/Data${sub}/$want"
    else
      echo "MISSING $want"; missing=$((missing + 1))
    fi
  done
}
say "linking the stock archives into $VIEW"
link ""      2 common.MPQ common-2.MPQ expansion.MPQ lichking.MPQ patch.MPQ patch-2.MPQ patch-3.MPQ
link "/enUS" 3 locale-enUS.MPQ expansion-locale-enUS.MPQ lichking-locale-enUS.MPQ \
                patch-enUS.MPQ patch-enUS-2.MPQ patch-enUS-3.MPQ

if [ "$missing" -gt 0 ]; then
  echo
  echo "$missing archive(s) missing. That stops the phase (GUIDE 6.1): ask the operator about their"
  echo "client. Usually it is not build 12340 enUS, or it is a partial copy."
  exit 1
fi
say "all twelve stock archives linked"
[ "$VIEW_ONLY" = "1" ] && exit 0

# --- 6.2 extract -------------------------------------------------------------
for tool in map_extractor vmap4_extractor vmap4_assembler mmaps_generator; do
  [ -x "$BIN/$tool" ] || { echo "$BIN/$tool is missing — build with -DTOOLS_BUILD=all (ops/build.sh)"; exit 1; }
done

mkdir -p "$DATA_DIR" "$LOGS_DIR/setup"
LOG="$LOGS_DIR/setup/extract-$(date +%Y%m%d-%H%M%S).log"

run_all() {
  cd "$DATA_DIR"
  say "map_extractor"      ; "$BIN/map_extractor" -i "$VIEW" -o .
  say "vmap4_extractor"    ; "$BIN/vmap4_extractor" -d "$VIEW/Data"
  say "vmap4_assembler"    ; mkdir -p vmaps && "$BIN/vmap4_assembler" Buildings vmaps
  # A few tiles dropped at the vertex limit is normal and is not an error.
  say "mmaps_generator"    ; "$BIN/mmaps_generator" --config "$BIN/mmaps-config.yaml" --threads "$THREADS"
  say "done; Buildings/ is an intermediate and can be deleted once the check passes"
}

if [ "$FOREGROUND" = "1" ]; then
  run_all 2>&1 | tee "$LOG"
else
  ( run_all ) > "$LOG" 2>&1 < /dev/null &
  pid=$!
  echo "$pid" > "$LOGS_DIR/setup/extract.pid"
  say "extracting, detached (pid $pid)"
  say "watch: tail -f $LOG"
  say "check when it ends: ls $DATA_DIR/dbc | wc -l   # expect 246"
fi
