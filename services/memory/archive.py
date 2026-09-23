#!/usr/bin/env python3
"""Keep a permanent copy of every bot memory (plan 41).

mod_ollama_chat_memories is NOT durable. Memory_SaveAll rewrites a dirty bot's
rows as DELETE-then-INSERT from RAM (mod-ollama-chat_memory.cpp:465-481), so the
table is a projection of the worldserver's memory, not a record. Anything RAM has
dropped -- an eviction at MaxPerBot, a bot whose state was erased on logout before
a save -- is destroyed on the next write.

Two things guard against that, and this script is the second:

  1. The AFTER DELETE trigger trg_ollama_mem_archive, which copies every removed
     row into mod_ollama_chat_memories_archive as it goes. That catches evictions
     the moment they happen. It is the primary guard.
  2. This sweeper, which INSERT IGNOREs the live table into the archive on an
     interval. It catches anything the trigger could not: rows written while the
     trigger was absent, a trigger dropped by a schema reload, or a restore of the
     live table from an older dump.

Dedup key is (bot_guid, created_at, MD5(memory_text)) -- NOT id. Because the save
path deletes and reinserts, AUTO_INCREMENT ids are regenerated constantly and the
same memory carries a different id every save cycle. created_at survives the
rewrite (FROM_UNIXTIME(m.createdAt)), which is what makes the key stable.

Usage:
  python3 archive.py once                  sweep a single time and report
  python3 archive.py run --interval 60     sweep forever (the service)
  python3 archive.py status                counts, drift, and trigger health
"""
import argparse
import re
import subprocess
import sys
import time

import os  # noqa: E402  (the loader below needs it; harmless if it is already imported above)

# Where this realm lives now comes from site/, with the live server config as the fallback (plan 23 W1).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from common import site  # noqa: E402

LIVE = "mod_ollama_chat_memories"
ARCH = "mod_ollama_chat_memories_archive"
TRIGGER = "trg_ollama_mem_archive"


def creds():
    """host, port, user, password, database for the character schema. Never printed."""
    return site.db("characters")


def sql(query, fetch=True):
    host, port, user, pw, db = creds()
    cmd = ["mysql", "--default-character-set=utf8mb4", "-h", host, "-P", port, "-u", user, db]
    if fetch:
        cmd.append("--batch")
    cmd += ["-e", query]
    out = subprocess.run(cmd, env={"MYSQL_PWD": pw, "PATH": "/usr/bin:/bin"},
                         capture_output=True)
    err = out.stderr.decode("utf-8", "replace")
    if err.strip() and "Using a password" not in err:
        raise RuntimeError(err.strip()[:400])
    rows = out.stdout.decode("utf-8", "replace").splitlines()
    return [r.split("\t") for r in rows[1:]] if fetch and rows else []


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def counts():
    r = sql(f"SELECT (SELECT COUNT(*) FROM {LIVE}), (SELECT COUNT(*) FROM {ARCH});")
    return (int(r[0][0]), int(r[0][1])) if r else (0, 0)


def sweep():
    """Copy anything live that the archive lacks. Idempotent."""
    before = counts()[1]
    sql(f"INSERT IGNORE INTO {ARCH} (bot_guid, memory_text, importance, created_at) "
        f"SELECT bot_guid, memory_text, importance, created_at FROM {LIVE};", fetch=False)
    after = counts()[1]
    return after - before


def cmd_once(_args):
    added = sweep()
    live, arch = counts()
    log(f"swept: +{added} newly archived; live={live} archive={arch}")
    return 0


def cmd_run(args):
    log(f"archiver up; sweeping every {args.interval}s")
    while True:
        try:
            added = sweep()
            if added:
                live, arch = counts()
                log(f"+{added} newly archived (live={live} archive={arch})")
        except Exception as exc:                      # never die on a transient DB blip
            log(f"sweep failed, will retry: {exc}")
        time.sleep(args.interval)


def cmd_status(_args):
    live, arch = counts()
    trig = sql("SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
               f"WHERE EVENT_OBJECT_TABLE = '{LIVE}' AND TRIGGER_NAME = '{TRIGGER}';".replace(
                   "{TRIGGER}", TRIGGER))
    log(f"live={live}  archive={arch}")
    log(f"capture trigger {TRIGGER}: {'PRESENT' if trig else 'MISSING -- evictions are being lost'}")
    # Archive holding more than live is the expected, healthy state: it means rows
    # that left the live table were caught rather than destroyed.
    held = sql(f"SELECT COUNT(*) FROM (SELECT bot_guid FROM {ARCH} GROUP BY bot_guid) t;")
    log(f"bots with archived memories: {held[0][0] if held else 0}")
    gone = sql(f"""SELECT COUNT(*) FROM {ARCH} a
                   WHERE NOT EXISTS (SELECT 1 FROM {LIVE} l
                     WHERE l.bot_guid = a.bot_guid AND l.created_at = a.created_at
                       AND MD5(l.memory_text) = a.text_hash);""")
    log(f"memories preserved that the live table no longer holds: {gone[0][0] if gone else 0}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("once").set_defaults(fn=cmd_once)
    r = sub.add_parser("run"); r.add_argument("--interval", type=int, default=60)
    r.set_defaults(fn=cmd_run)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
