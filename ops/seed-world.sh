#!/usr/bin/env bash
# seed-world.sh: Phases 10 and 11 in order, stopping at every gate (plan 23 W14).
#
#   ops/seed-world.sh [--from <step>] [--only <step>] [--list] [--dry-run]
#
# The generation steps are long and unattended; the gates are not. This runs the unattended ones in the
# right order, records each as it finishes so an interrupted run resumes instead of repeating, and
# HALTS at each operator gate with the exact command to hand over.
#
# It never runs sudo, never restarts the worldserver, and never decides anything that is the operator's
# (GUIDE rule 0.2.1, gates G3, G9, G10). A script that quietly passed its own gates would defeat them.
#
# Steps, in order:
#   freeze      10.1  count the guilds; gate: set BOT_GUILDS=0, conf-apply, restart          [GATE G3]
#   rag         10.2  rewrite the knowledge base in the world's voice, then trim it to the era
#   temperaments 10.3 load the in-world temperaments
#   lore        10.4  sample, generate, traits, crafts, restand, project, reload
#   main        10.5  name the main                                                          [GATE G10]
#   society     11.1-11.2  regard once, rivalry seed and ranks, fellowship, lore
#   services    11.3  install the user units                                                 [GATE G3]
#   court       11.4  the era's world overlay                                                [GATE G9]
set -uo pipefail
HERE=$(cd "$(dirname "$(realpath "$0")")" && pwd)
. "$HERE/env.sh"

STATE="${SITE_DIR}/seed-world.state"
STEPS=(freeze rag temperaments lore main society services court)
FROM=""; ONLY=""; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --from) FROM=${2:?--from needs a step}; shift ;;
    --only) ONLY=${2:?--only needs a step}; shift ;;
    --list) printf '%s\n' "${STEPS[@]}"; exit 0 ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,/^set -uo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1 (see --help)"; exit 2 ;;
  esac
  shift
done

: "${ERA:?ERA is not set in site/site.env}"
LORE="$REPO/services/lore"; REG="$REPO/services/regard"
say()  { echo "[$(date +%H:%M:%S)] $*"; }
done_already() { [ -f "$STATE" ] && grep -qx "$1" "$STATE"; }
mark() { mkdir -p "$(dirname "$STATE")"; echo "$1" >> "$STATE"; }
gate() {
  echo
  echo "──────── GATE $1 ────────"
  shift
  printf '%s\n' "$@"
  echo "Nothing further runs until that is done. Then: $0 --from $NEXT"
  echo "─────────────────────────"
  exit 10
}
run() { if [ "$DRY" = "1" ]; then echo "    would run: $*"; else "$@" || return 1; fi; }

step_freeze() {
  local n; n=$(db -N "${DB_CHARACTERS:-acore_characters}" -e "SELECT COUNT(*) FROM guild" 2>/dev/null || echo 0)
  say "guilds founded so far: $n (target ${BOT_GUILDS:-20})"
  if [ "${n:-0}" -lt "${BOT_GUILDS:-20}" ]; then
    echo "    not enough yet. Bots found guilds as they log in; wait, or continue anyway if the count"
    echo "    has not risen in 30 minutes (GUIDE 10.1)."
  fi
  NEXT=rag
  gate "G3 — freeze the companies" \
    "Stories and seats assume the rosters stay put, so the companies are frozen before any lore is written." \
    "" \
    "  1. set BOT_GUILDS=0 in site/site.env" \
    "  2. python3 ops/scripts/conf-apply.py --only playerbots" \
    "  3. the operator runs:  sudo systemctl restart wow-world"
}

step_rag() {
  run python3 "$LORE/rag_inworld.py" inworld --route chronicle --workers 2 || return 1
  run python3 "$LORE/rag_inworld.py" revise --era "$ERA" --backup-dir "${BACKUP_DIR:-$REPO/backups}/rag-$(date +%F)"
}

step_temperaments() {
  # NOT `bash -c "db ... < file"`: db is a shell function from ops/env.sh, and bash -c starts a new
  # shell that inherits no functions. It would fail with "db: command not found" on a real run while
  # looking perfectly fine in --dry-run, which is how it got written that way in the first place.
  local f="$REPO/sql/characters/temperaments-inworld.sql"
  [ -f "$f" ] || { echo "    missing $f"; return 1; }
  if [ "$DRY" = "1" ]; then
    echo "    would run: db ${DB_CHARACTERS:-acore_characters} < $f"
    return 0
  fi
  db "${DB_CHARACTERS:-acore_characters}" < "$f"
}

step_lore() {
  say "read the sample yourself before the rest (GUIDE 10.4): game words, numbers, later-era names"
  run python3 "$LORE/gen_backstories.py" sample --era "$ERA" || return 1
  run python3 "$LORE/gen_backstories.py" generate --era "$ERA" --unguilded || return 1
  run python3 "$LORE/gen_backstories.py" traits --era "$ERA" || return 1
  run python3 "$LORE/gen_backstories.py" crafts --era "$ERA" || return 1
  run python3 "$LORE/gen_backstories.py" traits-report --era "$ERA" || true
  run python3 "$LORE/gen_backstories.py" restand --era "$ERA" || return 1
  run python3 "$LORE/gen_backstories.py" project || return 1
  # Same reason as step_temperaments: ra is a function from ops/env.sh, so it is called directly.
  if [ "$DRY" = "1" ]; then
    echo '    would run: ra "ollama reload"'
  else
    ra "ollama reload" || say "could not reload over the GM console; a restart also picks it up"
  fi
}

step_main() {
  NEXT=society
  gate "G10 — name the main" \
    "Ask the operator which character they play as themselves, then name it:" \
    "" \
    "  GATE=\"http://\${LORE_GATE_BIND:-127.0.0.1}:\${LORE_GATE_PORT:-8788}\"" \
    "  curl -s -H \"X-Lore-Token: \$LORE_GATE_TOKEN\" \"\$GATE/claim\"" \
    "  curl -s -X POST \"\$GATE/main-set\" -H \"X-Lore-Token: \$LORE_GATE_TOKEN\" \\" \
    "       -H 'Content-Type: application/json' -d '{\"character\": \"<name>\"}'" \
    "" \
    "Then write the alts:  python3 services/lore/gen_backstories.py alts --era $ERA" \
    "" \
    "A realm with no player character is a real thing to want. If that is the case here, say so out" \
    "loud and write \"no main by choice\" in site/SETUP-STATE.md, so no later session re-asks."
}

step_society() {
  run python3 "$REG/regard.py" once || return 1
  run python3 "$REG/rivalry.py" seed --era "$ERA" || return 1
  run python3 "$REG/rivalry.py" ranks --era "$ERA" || return 1
  run python3 "$REG/society.py" fellowship || return 1
  run python3 "$REG/society.py" lore || return 1
}

step_services() {
  run "$REPO/ops/systemd/install.sh" --user || return 1
  NEXT=court
  gate "G3 — start the services" \
    "The units are installed. The operator runs:" \
    "" \
    "  sudo loginctl enable-linger ${WOW_USER:-<user>}" \
    "" \
    "Then, as that user:" \
    "  systemctl --user enable --now wow-regard wow-chronicler wow-lore-gate wow-market wow-memory-archive" \
    "  systemctl --user enable --now wow-backup.timer"
}

step_court() {
  NEXT=done
  gate "G9 — the era's world overlay" \
    "This edits the world database: Bolvar and Prestor into Stormwind Keep, later-age figures parked," \
    "their quests moved. It has a revert file. Back up first — the operator approves this one." \
    "" \
    "  mkdir -p \"\$BACKUP_DIR\" && MYSQL_PWD=\"\$DB_PASS\" mysqldump --default-character-set=utf8mb4 \\" \
    "    -h\"\$DB_HOST\" -u\"\$DB_USER\" \"\$DB_WORLD\" | gzip > \"\$BACKUP_DIR/world-before-classic-court-\$(date +%F).sql.gz\"" \
    "  db \"\$DB_WORLD\" < sql/world/classic-court.sql" \
    "" \
    "Then the operator restarts:  sudo systemctl restart wow-world"
}

# ---------------------------------------------------------------------------
started=0
for s in "${STEPS[@]}"; do
  [ -n "$ONLY" ] && [ "$ONLY" != "$s" ] && continue
  if [ -z "$ONLY" ]; then
    [ -n "$FROM" ] && [ "$started" = "0" ] && [ "$FROM" != "$s" ] && continue
    started=1
    if done_already "$s"; then say "$s: already done (in $STATE); skipping"; continue; fi
  fi
  say "── $s"
  if "step_$s"; then
    [ "$DRY" = "1" ] || mark "$s"
  else
    echo
    echo "$s failed. Nothing after it has run. Fix the error above and resume with:"
    echo "    $0 --from $s"
    exit 1
  fi
done

if [ "$DRY" = "1" ]; then
  say "dry run: nothing was run, and nothing was recorded"
else
  say "recorded so far in $STATE: $(tr '\n' ' ' < "$STATE" 2>/dev/null || echo none)"
fi
