#!/usr/bin/env python3
"""Does bot grouping actually work? (plans/31 §15, §16)

There is no unit-test harness for playerbots: its predicates take Player*, Group* and world state, the
module is not built as a library, and the interesting ones cannot be run without a world. What there IS, is
a complete record of everything that happens -- mod-ledger writes every group join and leave with its
leader, every kill with its boss flag, every death, every loot and every zone change. So the question
"can a company of bots clear a dungeon and come out with loot" is not a thing that needs new
instrumentation. It is a query.

  wiring                  before waiting an hour: can this feature work at all as configured?
  pipeline [--since H]    the seven stages of a delve, and which one it stops at
  watch [--interval S]    the same, repeatedly, reporting only what changed

`wiring` is the one that catches the trap this plan fell into first: turning on RandomBotGroupNearby while
the strategy carrying the action is not registered with any bot, so nothing happens and nothing says why.
"""
import argparse
import os
import re
import subprocess
import sys
import time

import os  # noqa: E402  (the loader below needs it; harmless if it is already imported above)

# Where this realm lives now comes from site/, with the live server config as the fallback (plan 23 W1).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from common import site  # noqa: E402

WORLDSERVER_CONF = site.server_conf()
PLAYERBOTS_CONF = site.server_conf(os.path.join("modules", "playerbots.conf"))
PB_SRC = os.path.join(site.get("CORE_DIR", "/opt/wow/azerothcore-wotlk"),
                      "modules", "mod-playerbots", "src")

_DB = site.db("characters")


def sql(query):
    host, port, user, pw, db = _DB
    out = subprocess.run(["mysql", "--default-character-set=utf8mb4", "-h", host, "-P", port, "-u", user,
                          db, "--batch", "--raw", "-N"], input=query,
                         env={"MYSQL_PWD": pw, "PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return [r.split("\t") for r in out.stdout.splitlines() if r]


def conf(key, path=PLAYERBOTS_CONF):
    pat = re.compile(r"^\s*%s\s*=\s*(.*?)\s*$" % re.escape(key), re.M)
    try:
        m = pat.search(open(path, encoding="utf-8", errors="replace").read())
    except OSError:
        return None
    return m.group(1).strip('"') if m else None


def src_has(path, needle, live_code=True):
    """Is `needle` in that source file? With live_code, a commented-out line does not count -- which is the
    whole point for addStrategy("group"). Patch markers live IN comments, so they pass live_code=False."""
    try:
        for line in open(os.path.join(PB_SRC, path), encoding="utf-8", errors="replace"):
            if needle in line and (not live_code or not line.lstrip().startswith("//")):
                return True
    except OSError:
        return None
    return False


OK, BAD, WARN = "  ok  ", " FAIL ", " warn "


def wiring(args):
    """Static checks. Every one of these was a real trap while this was investigated."""
    rows = []

    group_flag = conf("AiPlayerbot.RandomBotGroupNearby")
    strategies = conf("AiPlayerbot.RandomBotNonCombatStrategies") or ""
    registered = src_has("Bot/Factory/AiFactory.cpp", 'addStrategy("group")')
    in_config = "+group" in strategies

    rows.append((OK if group_flag == "1" else WARN,
                 "RandomBotGroupNearby", group_flag,
                 "bots may invite nearby bots" if group_flag == "1" else "OFF: InviteNearbyToGroupAction returns false"))

    rows.append((OK if (registered or in_config) else BAD,
                 "GroupStrategy reaches a bot",
                 f"AiFactory={'yes' if registered else 'commented out'}, config={'+group' if in_config else 'absent'}",
                 "the strategy carrying 'invite nearby' is registered"
                 if (registered or in_config) else
                 "NOTHING registers it: the flag above can do nothing. Add +group to "
                 "AiPlayerbot.RandomBotNonCombatStrategies"))

    # The two together are the whole feature; either alone is inert.
    rows.append((OK if (group_flag == "1" and (registered or in_config)) else BAD,
                 "both halves present", f"{group_flag} / {'yes' if (registered or in_config) else 'no'}",
                 "grouping can happen" if (group_flag == "1" and (registered or in_config))
                 else "grouping CANNOT happen as configured"))

    # H1: is the local fix in? Without it a bot-only group works only by accident.
    h1 = src_has("Bot/PlayerbotAI.cpp", "plans/31 \u00a715 H1", live_code=False)
    rows.append((OK if h1 else WARN, "H1 bot leader may be master", "patched" if h1 else "upstream",
                 "FindNewMaster returns the leader for a bot-only company" if h1
                 else "FindNewMaster returns nullptr; group holds together by accident"))

    # H3: can a person recruit a bot that is already in a company? Two answers, and the hook is the good
    # one -- it fires only when an invite is actually sent, where the yield rule dissolves companies near
    # the player whether he wants anyone or not. Either alone is enough.
    yield_chance = conf("AiPlayerbot.YieldGroupToPlayerChance")
    hook = src_has("Script/Playerbots.cpp", "OnPlayerCanGroupInvite")
    yields = src_has("Ai/Base/Actions/LeaveGroupAction.cpp", "plans/31 \u00a715 H3", live_code=False) \
        and yield_chance not in (None, "0")
    rows.append((OK if (hook or yields) else BAD,
                 "H3 a person can recruit a grouped bot",
                 f"invite hook={'in' if hook else 'absent'}, yield chance={yield_chance}",
                 ("the invite stands the bot down from its company first" if hook else
                  "companies disperse when someone walks over")
                 if (hook or yields) else
                 "NOBODY can recruit a grouped bot: the core refuses with ERR_ALREADY_IN_GROUP_S"))

    # H4: +group brings invite guild with it.
    # Informational, not a fault: it is a thing to expect, not a thing to fix.
    rows.append((OK, "H4 guild parties come too", "GroupStrategy also fires 'invite guild'",
                 "expect guild companies to form as well; unmeasured"
                 if (registered or in_config) else "not while GroupStrategy is unregistered"))

    # The dungeon path, which grouping alone does not give you.
    lfg = conf("AiPlayerbot.RandomBotJoinLfg")
    lo, hi = sql("SELECT SUM(level < 15), COUNT(*) FROM characters "
                 "WHERE account NOT IN (SELECT account_id FROM player_main)")[0]
    rows.append((OK if lfg == "1" else WARN, "LFG (the only way into an instance)", lfg,
                 f"{int(hi) - int(lo)} of {hi} bots are 15+ and eligible; {lo} are not"))

    print(f"{'':6} {'check':<34} {'value':<42} note")
    worst = 0
    for state, name, value, note in rows:
        print(f"[{state}] {name:<34} {str(value):<42} {note}")
        worst = max(worst, 2 if state == BAD else 1 if state == WARN else 0)
    print()
    print({0: "wiring looks coherent.", 1: "wiring is coherent but the feature is off or partial.",
           2: "WIRING IS BROKEN: turning things on will do nothing, silently."}[worst])
    return worst if args.strict else 0


# --- the delve pipeline ----------------------------------------------------
# Each stage is a strictly harder thing than the one before, so the first zero localises the failure.

# Whoever has named a main, not one hardcoded id (plan 43 H4). A realm with two people had the second
# counted among the bots by every stage below.
PLAYER_ACCOUNTS_SQL = "SELECT account_id FROM player_main"


def pipeline(args):
    since = f"ts > NOW() - INTERVAL {int(args.since)} HOUR"
    mine = f"(SELECT guid FROM characters WHERE account IN ({PLAYER_ACCOUNTS_SQL}))"
    instances = ",".join(str(m) for m in INSTANCE_MAPS)
    # Without this every stage below passes on the operator's own Deadmines run: his companions are bots,
    # and "not one of his characters" is not the same as "no person was there".
    alone = (f"AND NOT EXISTS (SELECT 1 FROM ledger_event p WHERE p.actor_guid IN {mine} "
             f"AND p.map_id = e.map_id AND p.ts BETWEEN e.ts - INTERVAL 30 MINUTE AND e.ts + INTERVAL 30 MINUTE)")

    stages = [
        ("1. a company forms with no person in it",
         f"SELECT COUNT(*) FROM ledger_event WHERE event_type='group_join' AND {since} "
         f"AND actor_guid NOT IN {mine} AND COALESCE(subject_guid,0) NOT IN {mine}"),
        ("2. it holds together past ten minutes",
         f"SELECT COUNT(*) FROM (SELECT j.subject_guid g, MIN(j.ts) a, "
         f"  COALESCE((SELECT MIN(l.ts) FROM ledger_event l WHERE l.event_type='group_leave' "
         f"            AND l.ts > MIN(j.ts) AND l.actor_guid=j.subject_guid), NOW()) b "
         f"  FROM ledger_event j WHERE j.event_type='group_join' AND {since} "
         f"  AND j.actor_guid NOT IN {mine} AND COALESCE(j.subject_guid,0) NOT IN {mine} "
         f"  GROUP BY j.subject_guid) t WHERE TIMESTAMPDIFF(MINUTE, a, b) >= 10"),
        ("3. someone in it reaches an instance",
         f"SELECT COUNT(DISTINCT e.actor_guid) FROM ledger_event e WHERE e.{since} AND e.map_id IN ({instances}) "
         f"AND e.actor_guid NOT IN {mine} {alone}"),
        ("4. two or more of them are inside together",
         f"SELECT COUNT(*) FROM (SELECT e.map_id, COUNT(DISTINCT e.actor_guid) n FROM ledger_event e "
         f"WHERE e.{since} AND e.map_id IN ({instances}) AND e.actor_guid NOT IN {mine} {alone} "
         f"GROUP BY e.map_id HAVING n >= 2) t"),
        ("5. they kill things in there",
         f"SELECT COUNT(*) FROM ledger_event e WHERE e.{since} AND e.map_id IN ({instances}) "
         f"AND e.event_type='kill' AND e.actor_guid NOT IN {mine} {alone}"),
        ("6. a master of the place falls to them",
         f"SELECT COUNT(*) FROM ledger_event e WHERE e.{since} AND e.map_id IN ({instances}) "
         f"AND e.event_type='kill' AND JSON_EXTRACT(e.detail,'$.boss')=1 AND e.actor_guid NOT IN {mine} {alone}"),
        ("7. and they come out with loot",
         f"SELECT COUNT(*) FROM ledger_event e WHERE e.{since} AND e.map_id IN ({instances}) "
         f"AND e.event_type='loot_item' AND e.actor_guid NOT IN {mine} {alone}"),
    ]

    print(f"A company of bots clearing a dungeon, over the last {args.since}h.")
    print("Each stage is harder than the one before, so the first zero is where it stops.\n")
    stopped = None
    for label, q in stages:
        n = int(sql(q)[0][0] or 0)
        mark = "ok  " if n else "STOP"
        print(f"  [{mark}] {label:<44} {n}")
        if not n and stopped is None:
            stopped = label
    print()
    if stopped is None:
        print("Every stage reached. Bot companies are clearing dungeons and taking loot.")
        return 0
    print(f"Stops at: {stopped}")
    print("  " + DIAGNOSIS.get(stopped[:2], "")) 
    return 1 if args.strict else 0


DIAGNOSIS = {
    "1.": "No bot-only company has ever formed. Run `wiring` -- this is a configuration answer, not a behaviour one.",
    "2.": "Companies form and dissolve fast. Look at LeaveFarAwayAction: deaths > 4, a level gap over 4, or drift "
          "beyond twice rpgDistance. Also check whether the leader is being rotated out of world "
          "(MinRandomBotInWorldTime).",
    "3.": "Companies hold but never travel to a dungeon. Nothing in playerbots decides to go to one: LFG is the "
          "only path in, it needs level 15+, and no bot on this realm has ever used it. THIS IS THE REAL GAP.",
    "4.": "Bots reach instances alone, not together. The group is not being kept intact through the portal.",
    "5.": "They get inside and do not fight. Check the follower strategies (plans/31 §15 H2): every addStrategy "
          "in AiFactory's grouped branch is commented out.",
    "6.": "They clear trash but no boss falls. Tuning, gear or pathing -- the first real content question.",
    "7.": "Bosses die but nothing is looted. Check loot rules for bot-only groups.",
}

INSTANCE_MAPS = [36, 34, 43, 47, 129, 33, 48, 90, 189, 70, 209, 349, 109, 230, 229, 429, 289, 329, 389,
                 409, 249, 469, 309, 509, 531, 533]


def watch(args):
    last = None
    while True:
        out = subprocess.run([sys.executable, __file__, "pipeline", "--since", str(args.since)],
                             capture_output=True, text=True).stdout
        line = next((l for l in out.splitlines() if l.startswith("Stops at:")), "all stages reached")
        if line != last:
            print(f"[{time.strftime('%H:%M:%S')}] {line}", flush=True)
            last = line
        time.sleep(args.interval)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("wiring", help="can the feature work at all as configured?")
    w.add_argument("--strict", action="store_true", help="exit non-zero on a failure")
    p = sub.add_parser("pipeline", help="the seven stages of a bot-run delve")
    p.add_argument("--since", type=int, default=24, help="hours to look back")
    p.add_argument("--strict", action="store_true")
    t = sub.add_parser("watch")
    t.add_argument("--since", type=int, default=24)
    t.add_argument("--interval", type=int, default=300)
    args = ap.parse_args()
    return dict(wiring=wiring, pipeline=pipeline, watch=watch)[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
