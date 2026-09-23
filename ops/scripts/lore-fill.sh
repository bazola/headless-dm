#!/usr/bin/env bash
# Write Classic lore for every Classic-legal bot that lacks it, project it into mod-ollama-chat,
# reload the module over the GM console, and report coverage and server load.
# Loops generate until nothing is missing (the bot factory can add characters mid-run).
# GM console credentials: see ra.sh. Without them the reload is skipped and printed as a to-do.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
# Paths and credentials come from site/ now, not from a hardcoded root and a grep (plan 23 W1).
. "$HERE/../env.sh"
cd "$REPO/services/lore"
q() { db -N "${DB_CHARACTERS:-acore_characters}" -e "$1" 2>/dev/null; }
# kind='bot', not ('bot','alt'): this count drives `generate`, which refuses alts, so counting them would loop forever.
missing() { q "SELECT COUNT(*) FROM characters c JOIN person_kind pk ON pk.guid=c.guid LEFT JOIN lore_character l ON l.guid=c.guid WHERE pk.kind='bot' AND c.race NOT IN (10,11) AND c.class<>6 AND l.guid IS NULL"; }
for pass in 1 2 3; do
  m=$(missing); echo "pass $pass: $m Classic bots without a backstory"
  [ "$m" = "0" ] && break
  mkdir -p "${LOGS_DIR:-$REPO/logs}"
  L="${LOGS_DIR:-$REPO/logs}/gen-${ERA:-classic}-fill-$(date +%Y%m%d-%H%M%S).log"
  python3 gen_backstories.py generate --era classic --unguilded < /dev/null > "$L" 2>&1
  echo "  generate exit=$? log=$L"; grep -E "pool finished|slots [0-9]|done:|FAILED|skipped until" "$L" | sed 's/^/  /'
done
# Personality and motivation for the new stories (plan 15), then fit every story to where its character stands now:
# level brackets re-roll bots to other levels, and a story written for a veteran should not follow a recruit.
python3 gen_backstories.py traits --era classic < /dev/null 2>&1 | grep -E "done:|FAILED" | sed 's/^/  /'
python3 gen_backstories.py restand --era classic < /dev/null 2>&1 | grep -E "characters:|revised|FAILED" | sed 's/^/  /'
# A trade and a household (plan 33), read out of character_skills and written against the story.
# After restand, never before: restand rewrites the backstory, and these are generated against it --
# kin_state tells the model which relatives the story already buried, and the gazetteer allows any place
# the story already names. Generate them first and restand pulls that source out from under them.
# zb-qwen80 is excluded deliberately: measured 2026-09-21 at a 74.6% rejection rate and, worse, it names
# a trade the character does not hold in 12% of what it does get accepted (plan 33 s6.1).
# Its own line, never chained: crafts exits non-zero when any character fails, and fill-only means the
# next run simply retries them.
python3 gen_backstories.py crafts --era classic --slots zb-qwen80=0 < /dev/null 2>&1 | grep -E "done:|FAILED" | sed 's/^/  /'
# The player's own alts (plan 21 §4), bonded to their main. Plan 21 §4 always claimed this ran here and it
# never did, which is why alts could sit with no bond, no story and no personality row at all (25 #69).
# Not fatal when a realm has no main yet: `alts` exits saying so, and naming one is GUIDE 10.5.
python3 gen_backstories.py alts --era classic < /dev/null 2>&1 | grep -E "done:|FAILED|no alts found" | sed 's/^/  /'
python3 gen_backstories.py project < /dev/null 2>&1 | tail -1
if "$HERE/ra.sh" "ollama reload" "server info" 2>/dev/null | grep -q .; then
  "$HERE/ra.sh" "server info" | grep -E "Characters in world|Update time diff|Mean|Percentiles"
else
  echo "GM console credentials not set: run '.ollama reload' on the console to load the new backstories"
fi
echo "final: $(missing) Classic bots without a backstory; $(q "SELECT COUNT(*) FROM mod_ollama_chat_personality WHERE personality LIKE 'BIO\\_%'") BIO assignments; $(q "SELECT COUNT(*) FROM guild g LEFT JOIN lore_guild l ON l.guildid=g.guildid WHERE l.guildid IS NULL") guilds without history; $(q "SELECT COUNT(*) FROM characters WHERE online=1") online"
