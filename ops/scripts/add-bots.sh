#!/usr/bin/env bash
# add-bots.sh: add a batch of random bots (default 100) and make every one of them whole.
#
#   1. Raises AiPlayerbot.MinRandomBots and MaxRandomBots by the batch in the live playerbots.conf
#      (backup first), records conf/playerbots.overrides and commits it.
#   2. Restarts the worldserver (sudo). At startup the bot factory sizes random-bot accounts to
#      ceil(Max / 9) and fills each new account with one character per class: 9 Classic-legal, plus a
#      death knight that stays parked. The stored online target (bot_count) is cleared so the manager
#      draws a new one inside the new Min..Max.
#   3. Waits for the world to come up, the logins to level off and one level-bracket pass (300 s), so
#      new bots stand at the levels the bracket weights give them before anyone writes their story.
#   4. Runs lore-fill.sh: backstory, personality and motivation, restand, project (and a module reload
#      over the GM console when credentials are set).
#   5. Reports online bots against the bracket weights and how whole the new characters are.
#
# New bots join no company (RandomBotGuildCount = 0; the 20 companies are frozen, plan 18). Feelings
# about them grow from presence in the regard cycle; nothing needs seeding.
#
# Usage: add-bots.sh [--count N] [--yes] [--dry-run] [--resume] [--report] [--force] [--commit]
#   --count N   bots to add (default 100)
#   --commit    record conf/playerbots.overrides as a local commit. Without it nothing touches git.
#               This script never pushes: what leaves this machine is the operator's call (plan 23 W10).
#   --yes       don't ask before restarting the worldserver
#   --dry-run   print what would change and stop
#   --resume    skip the raise and the restart: wait for logins, write lore and report (after an interrupted run)
#   --report    only print the report for the last batch
#   --force     raise again even though the last raise hasn't been through a restart yet
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
# Where this realm lives (plan 23 W1). Sourced, never required: every path below keeps the default it
# had before, so a tree with no site/site.env runs exactly as it used to.
. "$HERE/../env.sh"
REPO=$(cd "$HERE/../.." && pwd)
CONF=${CONF:-${SERVER_PREFIX:-/opt/wow/server}/etc/modules/playerbots.conf}
SERVER_LOG=${SERVER_LOGS:-${SERVER_PREFIX:-/opt/wow/server}/logs}/Server.log
BACKUPS=${BACKUP_DIR:-/opt/wow/backups}
STATE=${STATE:-$BACKUPS/add-bots-last.env}
CHARS_PER_ACCOUNT=9   # RandomPlayerbotFactory::CalculateAvailableCharsPerAccount with no death knights

COUNT=100 YES=0 DRY=0 RESUME=0 REPORT=0 FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --count) COUNT=${2:?--count needs a number}; shift ;;
    --yes) YES=1 ;;
    --dry-run) DRY=1 ;;
    --resume) RESUME=1 ;;
    --report) REPORT=1 ;;
    --force) FORCE=1 ;;
    --commit) COMMIT=1 ;;
    -h|--help) sed -n '2,/^set -uo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1 (see --help)"; exit 2 ;;
  esac
  shift
done
case "$COUNT" in ''|*[!0-9]*|0) echo "--count must be a positive number"; exit 2 ;; esac

# Paths and credentials come from site/ now, not from a hardcoded root and a grep (plan 23 W1).
. "$HERE/../env.sh"
q() { db -N "${DB_CHARACTERS:-acore_characters}" -e "$1" 2>/dev/null; }
conf_get() { grep -E "^AiPlayerbot\.$1[[:space:]]*=" "$CONF" | tail -1 | sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]*$//'; }
conf_set() { sed -i -E "s/^(AiPlayerbot\.$1[[:space:]]*=[[:space:]]*).*/\1$2/" "$CONF"; }
say() { echo "[$(date +%H:%M:%S)] $*"; }

CLASSIC="c.race NOT IN (10,11) AND c.class <> 6"
# Schema names from site/, like db-init.sh and backup.sh. Hardcoded ones make every query here fail
# silently for an operator who renamed them -- q() swallows stderr, so the arithmetic below would then
# be handed an empty string rather than a number.
AUTH_DB=${DB_AUTH:-acore_auth}
PB_DB=${DB_PLAYERBOTS:-acore_playerbots}
BOTS="characters c JOIN $AUTH_DB.account a ON a.id = c.account AND a.username LIKE 'RNDBOT%'"
TYPE1="JOIN $PB_DB.playerbots_account_type t ON t.account_id = c.account AND t.account_type = 1"
rnd_accounts() { q "SELECT COUNT(*) FROM $PB_DB.playerbots_account_type WHERE account_type = 1"; }
pool() { q "SELECT COUNT(*) FROM $BOTS $TYPE1 WHERE $CLASSIC"; }
online() { q "SELECT COUNT(*) FROM $BOTS WHERE c.online = 1"; }
bot_count() { q "SELECT value FROM $PB_DB.playerbots_random_bots WHERE owner = 0 AND bot = 0 AND event = 'bot_count'"; }
need_accounts() { echo $(( ($1 + CHARS_PER_ACCOUNT - 1) / CHARS_PER_ACCOUNT )); }

report() {
  [ -f "$STATE" ] && . "$STATE"
  local first=${FIRST_GUID:-0}
  echo
  echo "== online: $(online) random bots (target $(bot_count), Min $(conf_get MinRandomBots) / Max $(conf_get MaxRandomBots)); pool $(pool) Classic characters on $(rnd_accounts) accounts"
  echo "== online levels against the bracket weights:"
  local total; total=$(online); [ "${total:-0}" -gt 0 ] || total=1
  local i=1
  q "SELECT LEAST(c.level DIV 10, 5) + (c.level >= 60), COUNT(*) FROM $BOTS WHERE c.online = 1 GROUP BY 1 ORDER BY 1" |
  while read -r band n; do
    i=$((band + 1))
    local lo=$((band * 10)) hi=$((band * 10 + 9)) pct
    [ "$band" = 0 ] && lo=1
    [ "$band" = 6 ] && lo=60 && hi=60
    pct=$(conf_get "LevelBrackets.Alliance.Range$i.Pct"); pct=${pct:-11}
    printf '   %5s  %4d  %3d%%  (weight %s%%)\n' "$lo-$hi" "$n" $((100 * n / total)) "$pct"
  done
  if [ "$first" -gt 0 ]; then
    echo "== this batch: characters from guid $first on"
    q "SELECT COUNT(*), SUM($CLASSIC), SUM($CLASSIC AND c.online = 1), SUM($CLASSIC AND l.guid IS NOT NULL),
              SUM($CLASSIC AND l.personality IS NOT NULL), SUM($CLASSIC AND b.personality IS NOT NULL),
              SUM($CLASSIC AND c.level = 1), SUM(m.guid IS NOT NULL)
       FROM $BOTS LEFT JOIN lore_character l ON l.guid = c.guid LEFT JOIN guild_member m ON m.guid = c.guid
       LEFT JOIN mod_ollama_chat_personality b ON b.guid = c.guid AND b.personality LIKE 'BIO\\_%'
       WHERE c.guid >= $first" |
    while read -r all classic on story traits prompt lvl1 guilded; do
      echo "   $all created: $classic Classic-legal ($((all - classic)) parked), $on online"
      echo "   $story with a backstory, $traits with personality and motivation, $prompt assigned in prompts"
      echo "   $guilded in a company; $lvl1 still level 1 (never logged in yet: restand fits their story once they roll a level)"
    done
  fi
  local rss; rss=$(ps -o rss= -p "$(pgrep -x worldserver | head -1)" 2>/dev/null)
  [ -n "$rss" ] && echo "== worldserver RSS $((rss / 1024)) MB"
  if "$HERE/ra.sh" "server info" 2>/dev/null | grep -E "Characters in world|Update time diff|Mean|Percentiles"; then :
  else echo "== check the load before the next batch: '.server info' in game, mean diff under 50 ms (plan 13 §4)"; fi
}

[ "$REPORT" = 1 ] && { report; exit 0; }

exec 9>"$BACKUPS/add-bots.lock"
flock -n 9 || { echo "another add-bots.sh is running"; exit 1; }

if [ "$RESUME" = 0 ]; then
  MIN=$(conf_get MinRandomBots); MAX=$(conf_get MaxRandomBots)
  case "$MIN$MAX" in ''|*[!0-9]*) echo "can't read Min/MaxRandomBots from $CONF"; exit 1 ;; esac
  ACCTS=$(rnd_accounts); POOL=$(pool); ON=$(online)
  NEW_MIN=$((MIN + COUNT)); NEW_MAX=$((MAX + COUNT))
  NEED=$(need_accounts "$NEW_MAX"); ADD=$((NEED > ACCTS ? NEED - ACCTS : 0))
  FREE=$(q "SELECT COUNT(*) FROM playerbots_names n LEFT JOIN characters c ON c.name = n.name WHERE c.guid IS NULL")
  echo "now:   Min $MIN / Max $MAX; $ACCTS random-bot accounts holding $POOL Classic characters; $ON online"
  echo "after: Min $NEW_MIN / Max $NEW_MAX; $NEED accounts (+$ADD, about $((ADD * CHARS_PER_ACCOUNT)) Classic characters); pool about $((POOL + ADD * CHARS_PER_ACCOUNT))"
  if [ "$ACCTS" -lt "$(need_accounts "$MAX")" ] && [ "$FORCE" = 0 ]; then
    echo "The last raise (Max $MAX) hasn't been through a restart: it needs $(need_accounts "$MAX") accounts and there are $ACCTS."
    echo "Restart and run 'add-bots.sh --resume' to finish that batch, or pass --force to raise again."
    exit 1
  fi
  [ "$((POOL + ADD * CHARS_PER_ACCOUNT))" -lt "$NEW_MIN" ] &&
    echo "note: the pool falls short of Min; the online count tops out at the pool (old accounts hold parked characters)"
  [ "${FREE:-0}" -lt $((ADD * 20)) ] && { echo "only $FREE unused bot names left; the factory stops when they run out"; exit 1; }
  [ "$DRY" = 1 ] && { echo "(dry run: nothing changed)"; exit 0; }

  if [ "$YES" = 0 ]; then
    read -r -p "This restarts the worldserver; anyone in game is disconnected. Go on? [y/N] " ok
    case "$ok" in y|Y|yes) ;; *) echo "stopped: nothing changed"; exit 1 ;; esac
  fi
  sudo -v || { echo "sudo is needed to restart wow-world: nothing changed"; exit 1; }

  B=$BACKUPS/add-bots-$(date +%Y%m%d-%H%M%S); mkdir -p "$B" && cp -p "$CONF" "$B/" || { echo "backup failed: nothing changed"; exit 1; }
  FIRST_GUID=$(( $(q "SELECT COALESCE(MAX(guid), 0) FROM characters") + 1 ))
  printf 'FIRST_GUID=%s\nMIN=%s\nMAX=%s\nBACKUP=%s\nSTARTED=%s\n' "$FIRST_GUID" "$NEW_MIN" "$NEW_MAX" "$B" "$(date -Is)" > "$STATE"
  conf_set MinRandomBots "$NEW_MIN"; conf_set MaxRandomBots "$NEW_MAX"
  [ "$(conf_get MaxRandomBots)" = "$NEW_MAX" ] || { cp -p "$B/playerbots.conf" "$CONF"; echo "conf edit failed: restored"; exit 1; }
  say "playerbots.conf: Min $NEW_MIN / Max $NEW_MAX (backup $B)"

  python3 "$HERE/conf-overrides.py" > /dev/null
  # The new size is a fact about this machine, so it belongs in site.env too (plan 23 W10).
  if [ -n "${SITE_DIR:-}" ] && [ -f "$SITE_DIR/site.env" ]; then
    sed -i -E "s/^BOT_MIN=.*/BOT_MIN=$NEW_MIN/; s/^BOT_MAX=.*/BOT_MAX=$NEW_MAX/" "$SITE_DIR/site.env"
    grep -q '^BOT_MIN=' "$SITE_DIR/site.env" || printf 'BOT_MIN=%s\nBOT_MAX=%s\n' "$NEW_MIN" "$NEW_MAX" >> "$SITE_DIR/site.env"
    say "site.env: BOT_MIN $NEW_MIN / BOT_MAX $NEW_MAX"
  fi
  # This script never pushes (plan 23 W10). It runs unattended behind sudo, and what leaves the machine
  # is the operator's call, not a side effect of growing the bot count. --commit keeps even the local
  # commit deliberate.
  if [ "${COMMIT:-0}" != "1" ]; then
    git -C "$REPO" diff --quiet -- conf/playerbots.overrides ||
      say "conf/playerbots.overrides changed; commit it when you like (--commit does that here)"
  elif git -C "$REPO" diff --quiet -- conf/playerbots.overrides; then :
  elif git -C "$REPO" commit -q -m "conf: random bots Min $NEW_MIN / Max $NEW_MAX (add-bots.sh, +$COUNT)" -- conf/playerbots.overrides; then
    say "committed conf/playerbots.overrides (not pushed)"
  fi

  say "restarting wow-world"
  sudo systemctl stop wow-world || { echo "stop failed; the conf is raised: restart by hand, then run add-bots.sh --resume"; exit 1; }
  q "DELETE FROM acore_playerbots.playerbots_random_bots WHERE owner = 0 AND bot = 0 AND event = 'bot_count'"
  START=$(date +%s)
  sudo systemctl start wow-world || { echo "start failed: see journalctl -u wow-world, then run add-bots.sh --resume"; exit 1; }

  say "waiting for the world (the factory creates accounts and characters first)"
  until [ "$(stat -c %Y "$SERVER_LOG" 2>/dev/null || echo 0)" -ge "$START" ] && grep -q 'ready\.\.\.' "$SERVER_LOG"; do
    systemctl is-active -q wow-world || { echo "wow-world stopped during startup: see journalctl -u wow-world"; exit 1; }
    [ $(( $(date +%s) - START )) -gt 1800 ] && { echo "the world isn't up after 30 min: see $SERVER_LOG"; exit 1; }
    sleep 10
  done
  READY=$(date +%s)
  say "world up in $((READY - START)) s: $(grep -o '[0-9]* random bot accounts with [0-9]* characters available' "$SERVER_LOG" | tail -1)"
fi

systemctl is-active -q wow-world || { echo "wow-world isn't running"; exit 1; }
say "waiting for the logins to level off"
last=-1 flat=0 t0=$(date +%s)
while :; do
  n=$(online); target=$(bot_count)
  say "  $n online (target ${target:-not drawn yet})"
  if [ -n "$target" ] && [ "$n" -ge "$target" ]; then break; fi
  if [ "$n" = "$last" ]; then flat=$((flat + 1)); else flat=0; fi
  [ "$flat" -ge 3 ] && break
  [ $(( $(date +%s) - t0 )) -gt 1800 ] && { say "  still climbing after 30 min; going on"; break; }
  last=$n; sleep 30
done

if [ "$RESUME" = 0 ]; then
  wait_s=$(( READY + 330 - $(date +%s) ))
  if [ "$wait_s" -gt 0 ]; then
    say "waiting ${wait_s} s for a level-bracket pass, so stories are written for the levels bots will keep"
    sleep "$wait_s"
  fi
fi

say "writing lore for the new characters (lore-fill.sh)"
"$HERE/lore-fill.sh" || say "lore-fill.sh exited $?: run it again to finish"
report
