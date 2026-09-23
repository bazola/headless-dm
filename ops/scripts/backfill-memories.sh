#!/usr/bin/env bash
# Digest past deeds into bot memories (plans/38), for the outings the player was part of.
#
# Who keeps deed memories LIVE is a separate question, set by OllamaChat.Memory.EventCompanionsOnly in the
# module config (0 = the whole world). This script only writes the retrospective ones.
#
# WHY THIS STOPS THE SERVER. Memory_SaveAll deletes every dirty bot's rows and reinserts them from RAM
# (mod-ollama-chat_memory.cpp:444), and Memory_Load reads the table at startup ONLY. So rows written
# underneath a running worldserver are wiped the moment that bot next forms a memory -- and the shutdown
# save wipes them too, which is why "write it, then restart" does not work either. The only safe order is
# down, write, up: the memories are then read in at boot and the module owns them from there.
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
# Only safe here, with the realm down. Memory_SaveAll reinserts a dirty bot's rows from RAM, so a row
# deleted under a running server comes back -- and the shutdown save would undo it too.
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
