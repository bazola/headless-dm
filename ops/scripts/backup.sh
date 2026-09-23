#!/usr/bin/env bash
# backup.sh: the realm, in a form it can be restored from (plan 23 W13, §8).
#
#   ops/scripts/backup.sh [--dry-run] [--keep N] [--remote]
#
# Dumps the four databases, then copies the two things a dump cannot rebuild: site/ (this machine's
# answers, secrets included) and the live server configs. Code is in git; generated lore, feelings and
# chronicle live in the databases; the client and its extracted data are Blizzard's and are never copied.
#
# THE TARGET IS PRIVATE. site/secrets.env and the live confs carry the database password, the GM console
# account and the dashboard token, so the backup directory is created 700 and nothing here ever prints a
# secret. If you copy backups off the machine, copy them somewhere only you can read.
#
# Restoring is the reverse and is deliberately not automated: see docs/GUIDE.md Appendix B.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/../env.sh"

KEEP=14
DRY=0
REMOTE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --keep) KEEP=${2:?--keep needs a number}; shift ;;
    --remote) REMOTE=1 ;;
    -h|--help) sed -n '2,/^set -uo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1 (see --help)"; exit 2 ;;
  esac
  shift
done

: "${BACKUP_DIR:?BACKUP_DIR is not set in site/site.env}"
: "${DB_PASS:?DB_PASS is not set in site/secrets.env}"
say() { echo "[$(date +%H:%M:%S)] $*"; }

STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$BACKUP_DIR/realm-$STAMP"

if [ "$DRY" = "1" ]; then
  say "would write $OUT"
  say "  databases: ${DB_AUTH:-acore_auth} ${DB_CHARACTERS:-acore_characters} ${DB_WORLD:-acore_world} ${DB_PLAYERBOTS:-acore_playerbots}"
  say "  plus site/ and $SERVER_PREFIX/etc"
  say "  keeping the newest $KEEP, removing $(ls -1d "$BACKUP_DIR"/realm-* 2>/dev/null | head -n -"$KEEP" | wc -l) older"
  exit 0
fi

mkdir -p "$BACKUP_DIR" && chmod 700 "$BACKUP_DIR" || { echo "cannot make $BACKUP_DIR"; exit 1; }
mkdir -p "$OUT" && chmod 700 "$OUT" || exit 1

# --single-transaction so the world keeps running: InnoDB gives a consistent snapshot without locking
# anyone out. The characters schema is the one that matters -- it holds the lore, the regard, the ledger,
# the companies and the chronicle -- so its failure is the one that aborts the run.
fail=0
for db_name in "${DB_AUTH:-acore_auth}" "${DB_CHARACTERS:-acore_characters}" "${DB_WORLD:-acore_world}" "${DB_PLAYERBOTS:-acore_playerbots}"; do
  [ -n "$db_name" ] || continue
  say "dumping $db_name"
  if MYSQL_PWD="$DB_PASS" mysqldump --single-transaction --quick --default-character-set=utf8mb4 \
       -h"${DB_HOST:-127.0.0.1}" -P"${DB_PORT:-3306}" -u"${DB_USER:-acore}" "$db_name" \
       | gzip -1 > "$OUT/$db_name.sql.gz"; then
    say "  $(du -h "$OUT/$db_name.sql.gz" | cut -f1)"
  else
    say "  FAILED: $db_name"; fail=1
    [ "$db_name" = "${DB_CHARACTERS:-acore_characters}" ] && { echo "the characters dump failed; stopping"; exit 1; }
  fi
done

# site/ and the live confs: everything a rebuilt machine needs that git does not carry.
say "copying site/ and the live configs"
tar czf "$OUT/site.tar.gz" -C "$REPO" site 2>/dev/null || say "  no site/ to copy"
tar czf "$OUT/etc.tar.gz" -C "$SERVER_PREFIX" etc 2>/dev/null || say "  could not read $SERVER_PREFIX/etc (needs the server's user)"
printf 'realm=%s\nera=%s\nstamp=%s\nrepo_commit=%s\n' \
  "${REALM_NAME:-unknown}" "${ERA:-unknown}" "$STAMP" "$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo none)" \
  > "$OUT/MANIFEST"

# After everything is written, not before: an earlier chmod missed MANIFEST entirely, because the file
# did not exist yet. etc.tar.gz and site.tar.gz carry the database password and the console account.
chmod 600 "$OUT"/* 2>/dev/null

# Rotation by count, newest kept. Nothing is deleted until this run has written its own directory, so a
# failed backup never costs you the previous one.
old=$(ls -1d "$BACKUP_DIR"/realm-* 2>/dev/null | head -n -"$KEEP")
if [ -n "$old" ]; then
  while IFS= read -r d; do [ -n "$d" ] && rm -rf "$d" && say "removed $d"; done <<< "$old"
fi

if [ "$REMOTE" = "1" ]; then
  if [ -n "${BACKUP_REMOTE:-}" ]; then
    say "copying to $BACKUP_REMOTE"
    rsync -a --chmod=D700,F600 "$OUT" "$BACKUP_REMOTE/" || say "  rsync failed; the local copy stands"
  else
    say "--remote given but BACKUP_REMOTE is not set in site.env; the local copy stands"
  fi
fi

say "done: $OUT ($(du -sh "$OUT" | cut -f1))"
[ "$fail" = "0" ] || say "one or more non-critical dumps failed; see above"
exit 0
