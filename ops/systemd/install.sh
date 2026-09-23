#!/usr/bin/env bash
# install.sh: render the unit templates from site/site.env and install them (plan 23 W6).
#
#   sudo ops/systemd/install.sh            the three server units -> /etc/systemd/system
#        ops/systemd/install.sh --user     the six service units  -> ~/.config/systemd/user
#        ops/systemd/install.sh [--user] --dry-run    render to stdout, install nothing
#
# The units are *.service.in with @WOW_USER@, @SERVER_PREFIX@, @REPO@ and @SITE_DIR@ in them, so a
# checkout runs
# wherever it is put and as whoever owns it. Nothing is installed if a placeholder is left unresolved:
# a unit with @REPO@ still in its ExecStart fails at start time with a message about a missing file,
# which is a long way from the actual mistake.
#
# The user units need lingering to start at boot without a login:  loginctl enable-linger <user>
# wow-quality.container is NOT installed here: it names one machine's GPU, image and model file, and
# belongs with the fleet (ops/models), not with the services.
set -euo pipefail

HERE=$(cd "$(dirname "$(realpath "$0")")" && pwd)     # ops/systemd
REPO=$(cd "$HERE/../.." && pwd)
SITE_DIR=${SITE_DIR:-$REPO/site}

USER_MODE=0
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --user) USER_MODE=1 ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1 (see --help)"; exit 2 ;;
  esac
  shift
done

[ -f "$SITE_DIR/site.env" ] || { echo "no $SITE_DIR/site.env — copy site.example/site.env first (GUIDE Phase 2)"; exit 1; }
# shellcheck disable=SC1091
set -a; . "$SITE_DIR/site.env"; set +a
: "${WOW_USER:?WOW_USER is not set in site.env}"
: "${SERVER_PREFIX:?SERVER_PREFIX is not set in site.env}"

render() {  # render <template> -> stdout
  sed -e "s|@WOW_USER@|$WOW_USER|g" \
      -e "s|@SERVER_PREFIX@|$SERVER_PREFIX|g" \
      -e "s|@REPO@|$REPO|g" \
      -e "s|@SITE_DIR@|$SITE_DIR|g" "$1"
}

check() {   # refuse anything still holding a placeholder
  if grep -q '@[A-Z_]\+@' "$1"; then
    echo "unresolved placeholder in $(basename "$1"):"; grep -n '@[A-Z_]\+@' "$1" | sed 's/^/    /'
    return 1
  fi
}

if [ "$USER_MODE" = "1" ]; then
  SRC="$REPO/ops/user-units"
  DEST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  UNITS=(wow-regard.service wow-chronicler.service wow-market.service wow-lore-gate.service
         wow-memory-archive.service wow-backup.service)
  EXTRA=(wow-backup.timer)
else
  # A dry run reads templates and writes nothing, so it has no business demanding root: the whole point
  # of --dry-run is to let someone see what would be installed before they reach for sudo.
  if [ "$DRY" != "1" ]; then
    [ "$(id -u)" = 0 ] || { echo "run with sudo, or pass --user for the service units"; exit 1; }
    if pgrep -x worldserver >/dev/null || pgrep -x authserver >/dev/null; then
      echo "authserver/worldserver are running outside systemd; stop them first"; exit 1
    fi
  fi
  SRC="$HERE"
  DEST=/etc/systemd/system
  UNITS=(wow-auth.service wow-world.service wow-router.service)
  EXTRA=()
fi

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
for u in "${UNITS[@]}"; do
  in="$SRC/$u.in"
  [ -f "$in" ] || { echo "missing template: $in"; exit 1; }
  render "$in" > "$tmp/$u"
  check "$tmp/$u" || exit 1
done
for e in ${EXTRA[@]+"${EXTRA[@]}"}; do
  [ -f "$SRC/$e" ] && cp "$SRC/$e" "$tmp/$e"
done

if [ "$DRY" = "1" ]; then
  for f in "$tmp"/*; do echo "=== $(basename "$f")"; cat "$f"; echo; done
  echo "(dry run: nothing installed; would go to $DEST)"
  exit 0
fi

mkdir -p "$DEST"
install -m 644 "$tmp"/* "$DEST/"
echo "installed $(ls -1 "$tmp" | wc -l) unit(s) into $DEST"

if [ "$USER_MODE" = "1" ]; then
  systemctl --user daemon-reload
  echo "enable what you want running, for example:"
  echo "  systemctl --user enable --now wow-regard wow-chronicler wow-lore-gate wow-market wow-memory-archive"
  echo "  systemctl --user enable --now wow-backup.timer"
  echo "and, so they survive a reboot without you logging in:  loginctl enable-linger $USER"
else
  systemctl daemon-reload
  systemctl enable --now wow-auth.service wow-world.service
  systemctl enable wow-router.service
  systemctl --no-pager --lines=0 status wow-auth wow-world wow-router || true
fi
