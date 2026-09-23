#!/usr/bin/env bash
# db-init.sh: the database user and the four schemas (plan 23 W14, GUIDE Phase 5).
#
#   ops/db-init.sh [--print]
#
# Writes the SQL to a private file and hands the operator ONE command to run with sudo. It does not run
# sudo itself (GUIDE rule 0.2.1), and the password never reaches a command line, a log, or this output:
# it is expanded by the shell into a file created under umask 077.
#
# --print shows the SQL with the password replaced, so it can be read before it is run.
set -euo pipefail
HERE=$(cd "$(dirname "$(realpath "$0")")" && pwd)
. "$HERE/env.sh"

PRINT=0
[ "${1:-}" = "--print" ] && PRINT=1
[ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] && { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0; }

: "${DB_USER:?DB_USER is not set in site/site.env}"
: "${DB_PASS:?DB_PASS is not set in site/secrets.env — generate one there first (gate G4)}"
AUTH=${DB_AUTH:-acore_auth}
CHARS=${DB_CHARACTERS:-acore_characters}
WORLD=${DB_WORLD:-acore_world}
BOTS=${DB_PLAYERBOTS:-acore_playerbots}

OUT="${WOW_ROOT:-$REPO}/tmp/db-init.sql"
mkdir -p "$(dirname "$OUT")"

# utf8mb4 throughout, not utf8: the lore is full of names the three-byte set cannot hold, and a
# mis-set collation is only discovered weeks later as mangled apostrophes in a bot's backstory.
( umask 077; cat > "$OUT" <<SQL
CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS';
CREATE DATABASE IF NOT EXISTS $AUTH  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS $CHARS DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS $WORLD DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS $BOTS  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL PRIVILEGES ON $AUTH.*  TO '$DB_USER'@'localhost';
GRANT ALL PRIVILEGES ON $CHARS.* TO '$DB_USER'@'localhost';
GRANT ALL PRIVILEGES ON $WORLD.* TO '$DB_USER'@'localhost';
GRANT ALL PRIVILEGES ON $BOTS.*  TO '$DB_USER'@'localhost';
FLUSH PRIVILEGES;
SQL
)

if [ "$PRINT" = "1" ]; then
  sed "s/IDENTIFIED BY '.*'/IDENTIFIED BY '<the password in site\/secrets.env>'/" "$OUT"
  exit 0
fi

echo "wrote $OUT (mode $(stat -c '%a' "$OUT"))"
echo
echo "Give the operator this one command (it needs root; see GUIDE gate G3):"
echo
echo "    sudo mysql < $OUT"
echo
echo "Then delete it, because it holds the password:"
echo
echo "    shred -u $OUT   # or: rm -f $OUT"
echo
# Name the four schemas this run actually created. A wildcard guess ('acore%') prints nothing for an
# operator who set DB_AUTH and friends to something else -- reading as "Phase 5 did nothing" on a
# Phase 5 that worked.
echo "Afterwards this should print all four schema names ($AUTH, $CHARS, $WORLD, $BOTS):"
echo "    . ops/env.sh && db -N -e \"SELECT schema_name FROM information_schema.schemata WHERE schema_name IN ('$AUTH','$CHARS','$WORLD','$BOTS')\""
