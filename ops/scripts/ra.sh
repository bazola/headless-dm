#!/usr/bin/env bash
# ra.sh "cmd1" "cmd2" ...: run GM console commands over RA (127.0.0.1:3443) and print the replies.
# Credentials come from RA_USER / RA_PASS in the environment, or from $RA_ENV
# (default ~/.config/custom-wow/ra.env, chmod 600, lines RA_USER=... and RA_PASS=...).
#
# The console serves one session at a time, eats the first second of input as telnet
# negotiation, and only closes when told to quit, hence the pauses, `quit` and nc -q.
set -euo pipefail
RA_ENV=${RA_ENV:-$HOME/.config/custom-wow/ra.env}
[ -z "${RA_USER:-}" ] && [ -f "$RA_ENV" ] && . "$RA_ENV"
: "${RA_USER:?set RA_USER and RA_PASS, or create $RA_ENV}"
: "${RA_PASS:?set RA_USER and RA_PASS, or create $RA_ENV}"
{
  sleep 3; printf '%s\r\n' "$RA_USER"
  sleep 1; printf '%s\r\n' "$RA_PASS"
  sleep 2
  for c in "$@"; do printf '%s\r\n' "$c"; sleep 4; done
  printf 'quit\r\n'
} | timeout $((20 + 4 * $#)) nc -q 3 127.0.0.1 3443 | tr -d '\r' | sed 's/AC>/\n/g' \
  | grep -viE '^(Password|Username|Authentication Required|Bye|AzerothCore rev)|^\s*$' || true
