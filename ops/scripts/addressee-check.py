#!/usr/bin/env python3
"""Read back what the addressee resolver actually did (plan 25 §29's check).

The check this replaces was a pairing over `ledger_chat`: for each line a person
spoke that named nobody, did the bot they were mid-exchange with answer, or did a
different voice take it?  It read 11 of 21 wrong before the resolver shipped.

**That query no longer measures routing.**  Once companies began holding
conversations unattended, a day carries thousands of ambient lines, so "the bot
who spoke in the previous 60 s" is a random remark rather than the line being
answered.  Measured 2026-09-22, the same query over the same code:

    09-20    51 bot lines/day    23% went elsewhere
    09-22  9055 bot lines/day    96% went elsewhere

It tracks chat density, not addressing.  So the resolver now says what it did
instead of our inferring it: set `OllamaChat.Addressee.LogDecisions = 1`
(`.ollama reload`, no restart), play a session, and run this over the log.

    ops/scripts/addressee-check.py
    ops/scripts/addressee-check.py --since "2026-09-23 19:00:00"
    ops/scripts/addressee-check.py --log /opt/wow/server/logs/Server.log -v

The cases, as the resolver names them:

    named      the pass named a bot, and that bot was a candidate
    holder     the pass named nobody, and the person WAS mid-exchange -> it stays
    random     the pass named nobody, and there was no thread -> a random voice
    group      the line was aimed at the whole party -> 2-3 answer, staggered
    invented   every name it returned was made up -> fall back to all candidates
    fallback   the answer would not parse at all -> fall back to all candidates

`holder` vs `random` is the number item 54 was about: every `random` on a line
that had a live thread would be the old bug, and the resolver only reports
`random` when no holder was available at all.
"""

import argparse
import os
import re
import sys
from collections import Counter

LINE = re.compile(
    r"addressee: branch=(?P<branch>\S+) sender=(?P<sender>\S+) holder=(?P<holder>\S+) "
    r"candidates=(?P<candidates>\d+) chose=(?P<chose>\S*) spoke=(?P<spoke>\d+) cap=(?P<cap>\d+)"
)

# AzerothCore stamps most log lines "YYYY-MM-DD HH:MM:SS"; anything else just
# sorts as text, which is why --since is compared as a plain string.
STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def read(path, since, seen=None):
    """Decisions in `path`, oldest first.

    `seen["stamped"]` is set if ANY line in the file carried a timestamp. On this
    realm the worldserver's Server.log does not stamp its lines, so --since would
    match nothing and silently return the whole file; main() warns rather than
    over-report a window the caller thinks they narrowed.
    """
    for raw in open(path, encoding="utf-8", errors="replace"):
        stamp = STAMP.match(raw)
        if stamp and seen is not None:
            seen["stamped"] = True
        if since and stamp and stamp.group(1) < since:
            continue
        m = LINE.search(raw)
        if m:
            yield m.groupdict()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default="/opt/wow/server/logs/Server.log")
    ap.add_argument("--since", default=None,
                    help='only lines at or after this stamp, e.g. "2026-09-23 19:00:00"')
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print every decision")
    args = ap.parse_args()

    if not os.path.exists(args.log):
        sys.exit(f"no such log: {args.log}")

    seen = {"stamped": False}
    rows = list(read(args.log, args.since, seen))

    if args.since and not seen["stamped"]:
        print(f"warning: no timestamps in {args.log}, so --since narrowed nothing "
              f"and every decision in the file is counted below.\n"
              f"         Roll or move the log before the session instead.",
              file=sys.stderr)

    if not rows:
        sys.exit("No decisions in that log.\n"
                 "Is OllamaChat.Addressee.LogDecisions = 1, and has a session been played "
                 "since it was set?  (`.ollama reload` after changing it.)")

    branches = Counter(r["branch"] for r in rows)
    total = len(rows)

    print(f"{total} routed lines\n")
    print("case      count   share")
    for name in ("named", "holder", "random", "group", "invented", "fallback"):
        n = branches.get(name, 0)
        if n:
            print(f"{name:<9} {n:>5}   {100.0 * n / total:>5.1f}%")
    for name, n in sorted(branches.items()):
        if name not in ("named", "holder", "random", "group", "invented", "fallback"):
            print(f"{name:<9} {n:>5}   {100.0 * n / total:>5.1f}%   (unrecognised)")

    # The item 54 number: of the lines that named nobody, how many had a thread
    # to stay with?  `random` means no holder was available, which is the case
    # the old bug turned into a coin flip.
    unnamed = branches.get("holder", 0) + branches.get("random", 0)
    if unnamed:
        held = branches.get("holder", 0)
        print(f"\nnamed nobody: {unnamed}  ->  stayed with the thread {held} "
              f"({100.0 * held / unnamed:.0f}%), no thread to stay with {unnamed - held}")

    # Did the group branch actually get more than one voice out?
    grp = [r for r in rows if r["branch"] == "group"]
    if grp:
        spoke = Counter(int(r["spoke"]) for r in grp)
        avg = sum(int(r["spoke"]) for r in grp) / len(grp)
        print(f"\ngroup branch: {len(grp)} lines, {avg:.2f} voices each "
              f"({', '.join(f'{k}:{v}' for k, v in sorted(spoke.items()))})")

    # A holder that was named but did not speak means the governor refused it and
    # the line was handed on -- worth seeing, since it is the one path where the
    # thread is known and still does not answer.
    silent = [r for r in rows if r["branch"] == "holder" and int(r["spoke"]) == 0]
    if silent:
        print(f"\nholder chosen but silent: {len(silent)} "
              f"(governor refused; the line was handed to another candidate)")

    if args.verbose:
        print()
        for r in rows:
            print(f"  {r['branch']:<9} {r['sender']:<14} holder={r['holder']:<14} "
                  f"chose={r['chose']:<24} spoke={r['spoke']}/{r['cap']} "
                  f"of {r['candidates']}")


if __name__ == "__main__":
    main()
