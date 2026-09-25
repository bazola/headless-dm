#!/usr/bin/env bash
# Digest past deeds into bot memories (plans/38), for the outings the player was part of.
#
# Who keeps deed memories LIVE is a separate question, set by OllamaChat.Memory.EventCompanionsOnly in the
# module config (0 = the whole world). This script only writes the retrospective ones.
#
# THE REALM NO LONGER HAS TO BE DOWN FOR THIS (corrected 2026-09-25, plans/52 W1).
#
# What this said before was true when it was written and has been false since plan 41 M2 made the save
# INSERT-ONLY. The old warning claimed Memory_SaveAll deletes a dirty bot's rows and reinserts them from
# RAM, so anything written underneath a running worldserver was wiped. Read the module today and none of
# that holds:
#
#   * the only statements the module makes against mod_ollama_chat_memories are two SELECTs and one
#     INSERT (memory.cpp:275 startup load, :533 AppendBotSave, :612 per-bot load). No DELETE. No UPDATE.
#   * rows read back out of the table are marked persisted = true ("it came out of the table; never
#     insert it again"), and AppendBotSave skips them -- so a row this script writes, rewrites or
#     DELETES cannot be resurrected by the server.
#   * Memory_LoadBot re-reads on login, and a bot that logs out has its state erased, so the change
#     reaches each bot as the fleet rotates. A restart is an accelerant, never a requirement.
#
# The one real consequence of running live is LATENCY: a bot that stays logged in keeps its old copy in
# its head until it next rotates. Nothing is lost, it just has not noticed yet.
#
# The stop/start below is therefore no longer required. It is left in as belt-and-braces because this
# script is the one that writes thousands of rows at once, and because taking it out is a behaviour
# change nobody has asked for yet. To drop it: remove the "stopping wow-world" block, the bring_up
# function and its trap, and this paragraph with them.
#
# Usage:   ops/scripts/backfill-memories.sh
#          SINCE='2026-09-14 00:00:00' ops/scripts/backfill-memories.sh    # a different window
#
# It asks for the sudo password once (systemctl stop/start), and the realm is brought back up on every
# exit path, including a failed digest or a Ctrl-C.
set -u

HERE=$(cd "$(dirname "$(realpath "$0")")" && pwd)     # ops/scripts
REPO=$(cd "$HERE/../.." && pwd)
# SERVER_PREFIX comes from site/site.env; the default is the guide's own layout.
CONF=${SERVER_PREFIX:-/opt/wow/server}/etc/modules/mod_ollama_chat.conf
SINCE=${SINCE:-'2026-09-18 00:00:00'}
UNTIL=${UNTIL:-'2026-09-21 23:11:11'}

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

up_done=0
bring_up() {
    [ "$up_done" = 1 ] && return
    up_done=1
    say "starting wow-world again"
    sudo systemctl start wow-world \
        || echo "COULD NOT START -- run this yourself: sudo systemctl start wow-world"
}
trap bring_up EXIT INT TERM

say "stopping wow-world (it flushes its memories on the way down)"
sudo systemctl stop wow-world || { echo "could not stop the realm; nothing was written"; exit 1; }
sleep 3

say "re-enabling deed memories (fleet-wide; thin buffers are dropped, not digested)"
sed -i 's/^OllamaChat\.Memory\.EventEnable = 0$/OllamaChat.Memory.EventEnable = 1/' "$CONF"
grep -n '^OllamaChat\.Memory\.EventEnable' "$CONF"

say "pruning the junk the fleet-wide first run left behind"
# This is safe with the realm up too, and the reason it once was not is gone: a deleted row is not
# reinserted, because the copy in that bot's head is marked persisted and AppendBotSave skips it. The bot
# simply goes on using the deleted memory until it next logs out and back in.
cd "$REPO" || exit 1
python3 services/memory/recall.py prune-events --apply

say "dropping any second generation from an earlier double run"
cd "$REPO" || exit 1
python3 services/memory/recall.py prune-dupes --since "$SINCE" --until "$UNTIL" --apply

say "digesting deeds from $SINCE to $UNTIL"
cd "$REPO" || exit 1
python3 services/memory/recall.py events --since "$SINCE" --until "$UNTIL" --apply
rc=$?

bring_up

say "what landed (give the realm a moment to read them in)"
sleep 8
python3 services/memory/recall.py show --bot Kankleerish | head -12
echo
echo "Check the realm came up in the mode you wanted:"
echo "  grep 'deed memories are' /opt/wow/server/logs/Server.log"
echo "It should read 'kept by every bot in the world'. If it says 'kept by them alone',"
echo "then OllamaChat.Memory.EventCompanionsOnly is still 1 in the module config."

exit $rc
