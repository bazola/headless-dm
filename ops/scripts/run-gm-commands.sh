#!/usr/bin/env bash
# Feed gm-console-commands.txt to the worldserver GM console (RA, 127.0.0.1:3443).
# Asks for the GM account and password (not echoed, not kept in shell history).
# Each command waits a second so the console output stays readable; ~1 minute total.
set -euo pipefail
cmds="$(dirname "$0")/gm-console-commands.txt"
read -rp "GM account: " user
read -rsp "Password: " pass; echo
{
  # The console waits up to 1 s after connect and throws away anything that arrives in that
  # window as telnet negotiation, so the username must come later than that.
  sleep 3; printf '%s\r\n' "$user"
  sleep 1; printf '%s\r\n' "$pass"
  sleep 2
  while IFS= read -r line; do
    [ -n "$line" ] && printf '%s\r\n' "$line" && sleep 1
  done < "$cmds"
  sleep 10
  printf 'quit\r\n'
# -q: exit after the input ends. Without it nc keeps the session open, and the console serves one
# session at a time, so every later console connection hangs behind it.
} | nc -q 3 127.0.0.1 3443
