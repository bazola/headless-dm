#!/usr/bin/env python3
"""era-spawn-sweep.py: list later-expansion people and objects standing in the old world (maps 0 and 1).

Heuristic, for review at each era flip (plan 12, plan 22 X1):
  - creatures with entry >= 17000 (TBC and later; a few late-1.12 entries sit just above it),
    game objects with entry >= 182000;
  - visible in phase 1, spawned in normal mode, not tied to a game event (holidays are handled elsewhere);
  - triggers, bunnies, doodads and other invisible helpers left out.
Each spawn is labelled with its zone from the dashboard map manifest and tagged:
  system (battlemasters, barbers, inscription, guild vaults, mailboxes, portals, zeppelin and boat masters),
  later-quests (starts a quest above level 60), quests, service (vendor, trainer, flight), people.

Output is Blizzard-derived: print it or write it outside the repo.

  python3 ops/scripts/era-spawn-sweep.py                 # summary per zone
  python3 ops/scripts/era-spawn-sweep.py --zone Stormwind --full
  python3 ops/scripts/era-spawn-sweep.py --tsv /tmp/era.tsv
"""
import argparse
import collections
import json
import re
import subprocess
import sys

import os  # noqa: E402  (the loader below needs it; harmless if it is already imported above)

# Where this realm lives now comes from site/, with the live server config as the fallback (plan 23 W1).
# Two levels up from ops/scripts/ is the repo; the loader lives beside the services.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))), "services"))
from common import site  # noqa: E402

MANIFEST = os.path.join(site.get("DATA_DIR", "/opt/wow/server/data"), "dashboard-maps", "manifest.json")
PARKED_PHASE = 16384  # mod-progression-system's hidden phase

SYSTEM = re.compile(r"battlemaster|arena|barber|inscription|guild vault|mailbox|meeting stone|zeppelin|"
                    r"portal|lexicon of power|dockmaster|wintergrasp|experience eliminator|quartermaster|"
                    r"boat to|flight master|gryphon master|hippogryph master|bat handler", re.I)
HELPER = re.compile(r"^\[|^<|bunny|trigger|dummy|credit|target|invisible|stalker|controller|specimen|"
                    r"spell|visual|doodad|collision", re.I)


def world_db():
    """host, port, user, password, database for the world schema. Never printed."""
    return site.db("world")


def sql(query):
    host, port, user, pw, db = world_db()
    out = subprocess.run(["mysql", "--default-character-set=utf8mb4", "-h", host, "-P", port, "-u", user, db,
                          "--batch", "--raw", "-N"], input=query, capture_output=True, text=True,
                         env={"MYSQL_PWD": pw, "PATH": "/usr/bin:/bin"})
    if out.returncode != 0:
        sys.exit(out.stderr.strip())
    return [row.split("\t") for row in out.stdout.splitlines() if row]


def zone_finder():
    zones = [z for z in json.load(open(MANIFEST)) if z.get("zone") and z.get("virtual_map", -1) == -1]

    def find(m, x, y):
        best = None
        for z in zones:
            # WorldMapArea bounds: left/right are y, top/bottom are x.
            if z["map"] != m:
                continue
            if (min(z["left"], z["right"]) <= y <= max(z["left"], z["right"])
                    and min(z["top"], z["bottom"]) <= x <= max(z["top"], z["bottom"])):
                area = abs(z["left"] - z["right"]) * abs(z["top"] - z["bottom"])
                if best is None or area < best[0]:
                    best = (area, z["name"])
        return best[1] if best else f"map {m}"
    return find


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zone", help="only zones whose name contains this")
    ap.add_argument("--full", action="store_true", help="list every kind, not the top 20 per zone")
    ap.add_argument("--tsv", help="also write every spawn to this file")
    ap.add_argument("--min-creature", type=int, default=17000)
    ap.add_argument("--min-object", type=int, default=182000)
    args = ap.parse_args()

    npcs = sql(f"""
select c.guid, c.id, ct.name, ifnull(ct.subname,''), c.map, c.position_x, c.position_y, ct.npcflag,
  exists(select 1 from creature_queststarter s join quest_template q on q.ID=s.quest
         where s.id=ct.entry and (q.MinLevel>60 or q.QuestLevel>60)),
  exists(select 1 from creature_queststarter s where s.id=ct.entry)
from creature c join creature_template ct on ct.entry=c.id
where c.map in (0,1) and c.id>={args.min_creature} and (c.phaseMask & 1) and (c.spawnMask & 1)
  and ct.flags_extra & 128 = 0
  and not exists(select 1 from game_event_creature g where g.guid=c.guid)""")
    objs = sql(f"""
select g.guid, g.id, t.name, '', g.map, g.position_x, g.position_y, 0, 0, 0
from gameobject g join gameobject_template t on t.entry=g.id
where g.map in (0,1) and g.id>={args.min_object} and (g.phaseMask & 1) and (g.spawnMask & 1)
  and not exists(select 1 from game_event_gameobject e where e.guid=g.guid)""")

    find = zone_finder()
    by_zone = collections.defaultdict(collections.Counter)
    tags = collections.Counter()
    rows = []
    for kind, data in (("npc", npcs), ("obj", objs)):
        for guid, entry, name, sub, m, x, y, npcflag, later_q, any_q in data:
            if HELPER.search(name):
                continue
            label = name + (f" <{sub}>" if sub else "")
            if SYSTEM.search(label):
                tag = "system"
            elif later_q == "1":
                tag = "later-quests"
            elif any_q == "1":
                tag = "quests"
            elif kind == "npc" and int(npcflag) & (128 | 16 | 32 | 8192 | 65536):
                tag = "service"
            else:
                tag = "people" if kind == "npc" else "object"
            zone = find(int(m), float(x), float(y))
            if args.zone and args.zone.lower() not in zone.lower():
                continue
            by_zone[zone][(tag, kind, label, entry)] += 1
            tags[tag] += 1
            rows.append((zone, tag, kind, entry, label, guid, m, x, y))

    if args.tsv:
        with open(args.tsv, "w") as f:
            for r in sorted(rows):
                f.write("\t".join(map(str, r)) + "\n")

    print(f"{len(rows)} spawns in {len(by_zone)} zones; by tag: "
          + ", ".join(f"{t} {n}" for t, n in tags.most_common()))
    for zone, kinds in sorted(by_zone.items(), key=lambda kv: -sum(kv[1].values())):
        print(f"\n## {zone}: {sum(kinds.values())} spawns, {len(kinds)} kinds")
        order = {"later-quests": 0, "quests": 1, "people": 2, "service": 3, "system": 4, "object": 5}
        items = sorted(kinds.items(), key=lambda kv: (order[kv[0][0]], -kv[1]))
        for (tag, kind, label, entry), n in (items if args.full else items[:20]):
            print(f"  {tag:<12} {kind} {label} [{entry}]" + (f" x{n}" if n > 1 else ""))
        if not args.full and len(items) > 20:
            print(f"  … {len(items) - 20} more (--full)")


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # quiet when piped into head
    main()
