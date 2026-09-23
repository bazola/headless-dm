#!/usr/bin/env python3
"""The era's item list for the merchants' stalls (custom wow plans/17 §3.C).

The auction house module only filters by level, item level and ID ranges. Those let later-era items through (Outland
and Northrend drops with low requirements, later vendor and event goods in old-world cities), so this script lists
what the open world of the current era actually yields:

1. Open maps: Map.dbc expansion <= the era's, minus maps closed in `disables` (sourceType 2) - brackets not yet open.
2. Sourced: loot of creatures and game objects spawned on open maps (skinning and pickpocketing too, references
   followed), vendors spawned there, quest rewards from quest givers there, fishing in open zones, then what allowed
   containers and disenchanting yield. Spawns parked in the hidden phase or tied to a holiday are left out.
3. Crafted: what trainers on open maps teach up to the era's skill cap, and what allowed recipes teach.
4. Allowed = (sourced | crafted) with RequiredLevel <= level cap, ItemLevel <= the era's ceiling, entry below the
   era's first later-era entry.

Writes one item ID per line to --out (read by AuctionHouseBot.AllowedItemIDsFile) plus a report with spot checks.
Re-run at every bracket flip and after any acore_world restore, then restart or `.ahbot reload`.
Item IDs come from acore_world (Blizzard-derived): the output stays on this box.
"""
import argparse
import collections
import struct
import subprocess
import sys
import time
from pathlib import Path

import os  # noqa: E402  (the loader below needs it; harmless if it is already imported above)

# Where this realm lives now comes from site/, with the live server config as the fallback (23 W1/W16).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from common import site  # noqa: E402

_DATA = site.get("DATA_DIR", "/opt/wow/server/data")
DBC = Path(_DATA) / "dbc"
# The seller stops silently when this file cannot be read, so it is a site path, not a guess (W17).
DEFAULT_OUT = os.path.join(_DATA, "economy", "allowed-items.txt")

# expansion, level cap, item level ceiling, skill cap, first entry of the next era's items
ERAS = {
    "classic": dict(expansion=0, level=60, item_level=92, skill=300, max_entry=24000),
    "tbc": dict(expansion=1, level=70, item_level=164, skill=375, max_entry=34000),
    "wotlk": dict(expansion=2, level=80, item_level=284, skill=450, max_entry=1000000),
}

# (entry, why) spot checks for the classic report
MUST_BE_ABSENT = [(23424, "Fel Iron Ore"), (21877, "Netherweave Cloth"), (33470, "Frostweave Cloth"),
                  (23077, "Blood Garnet"), (25649, "Knothide Leather Scraps"), (44731, "heirloom-era item")]
MUST_BE_PRESENT = [(14047, "Runecloth"), (12360, "Arcanite Bar"), (13468, "Black Lotus"), (17010, "Fiery Core"),
                   (17011, "Lava Core"), (2589, "Linen Cloth")]
CLOSED_UNTIL_BRACKET = [(18562, "Elementium Ore (Blackwing Lair)"), (19726, "Bloodvine (Zul'Gurub)")]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


WORLD = site.db("world")


def rows(query):
    host, port, user, pw, db = WORLD
    out = subprocess.run(["mysql", "--default-character-set=utf8mb4", "-h", host, "-P", port, "-u", user, db,
                          "--batch", "--raw", "-N", "-e", query],
                         env={"MYSQL_PWD": pw, "PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return [line.split("\t") for line in out.stdout.splitlines() if line]


def ints(values):
    return ",".join(str(v) for v in sorted(values)) or "0"


def read_dbc(name):
    data = (DBC / name).read_bytes()
    magic, count, fields, size, _ = struct.unpack("<4s4I", data[:20])
    assert magic == b"WDBC" and size == fields * 4, name
    return [struct.unpack_from(f"<{fields}I", data, 20 + i * size) for i in range(count)]


def open_maps(era):
    expansion = {r[0]: r[63] for r in read_dbc("Map.dbc")}
    closed = {int(r[0]) for r in rows("SELECT entry FROM disables WHERE sourceType = 2")}
    return {m for m, e in expansion.items() if e <= era["expansion"]} - closed, closed


def loot(table):
    """Entry -> [(item, reference)] for a loot template."""
    out = collections.defaultdict(list)
    for entry, item, ref in rows(f"SELECT Entry, Item, Reference FROM {table}"):
        out[int(entry)].append((int(item), int(ref)))
    return out


def expand(entries, table, refs, seen_refs=None):
    """All items reachable from the given loot entries, following references."""
    items, stack = set(), [(table, e) for e in entries]
    seen = seen_refs if seen_refs is not None else set()
    while stack:
        tbl, entry = stack.pop()
        for item, ref in tbl.get(entry, ()) if isinstance(tbl, dict) else ():
            if ref:
                if ref not in seen:
                    seen.add(ref)
                    stack.append((refs, ref))
            elif item > 0:
                items.add(item)
    return items


def build(era_name):
    era = ERAS[era_name]
    maps, closed = open_maps(era)
    maplist = ints(maps)
    log(f"{len(maps)} open maps; closed by bracket or era: {sorted(closed)}")

    refs = loot("reference_loot_template")
    sourced = set()

    # Creatures on open maps, not parked, not tied to a holiday; plus what they summon from the database.
    creatures = {int(r[0]) for r in rows(
        f"SELECT DISTINCT c.id FROM creature c LEFT JOIN game_event_creature e ON e.guid = c.guid "
        f"WHERE c.map IN ({maplist}) AND c.phaseMask <> 16384 AND e.guid IS NULL")}
    summoned = {int(r[0]) for r in rows(
        f"SELECT DISTINCT action_param1 FROM smart_scripts WHERE source_type = 0 AND action_type = 12 "
        f"AND entryorguid IN ({ints(creatures)})")}
    summoned |= {int(r[0]) for r in rows(
        f"SELECT DISTINCT entry FROM creature_summon_groups WHERE summonerType = 0 AND summonerId IN ({ints(creatures)})")}
    creatures |= summoned
    log(f"{len(creatures)} creatures on open maps ({len(summoned)} summoned)")

    templates = rows(f"SELECT entry, lootid, skinloot, pickpocketloot FROM creature_template WHERE entry IN ({ints(creatures)})")
    for table, col in (("creature_loot_template", 1), ("skinning_loot_template", 2), ("pickpocketing_loot_template", 3)):
        ids = {int(t[col]) for t in templates if int(t[col])}
        got = expand(ids, loot(table), refs)
        log(f"{table}: {len(got)} items")
        sourced |= got

    objects = rows(
        f"SELECT DISTINCT t.type, t.Data1 FROM gameobject g JOIN gameobject_template t ON t.entry = g.id "
        f"LEFT JOIN game_event_gameobject e ON e.guid = g.guid "
        f"WHERE g.map IN ({maplist}) AND g.phaseMask <> 16384 AND e.guid IS NULL AND t.type IN (3, 25)")
    got = expand({int(o[1]) for o in objects if int(o[1])}, loot("gameobject_loot_template"), refs)
    log(f"gameobject_loot_template: {len(got)} items")
    sourced |= got

    vendors = {int(r[0]) for r in rows(f"SELECT DISTINCT item FROM npc_vendor WHERE item > 0 AND entry IN ({ints(creatures)})")}
    log(f"vendors: {len(vendors)} items")
    sourced |= vendors

    starters = {int(r[0]) for r in rows(f"SELECT quest FROM creature_queststarter WHERE id IN ({ints(creatures)})")}
    gobjects = {int(r[0]) for r in rows(
        f"SELECT DISTINCT g.id FROM gameobject g LEFT JOIN game_event_gameobject e ON e.guid = g.guid "
        f"WHERE g.map IN ({maplist}) AND g.phaseMask <> 16384 AND e.guid IS NULL")}
    starters |= {int(r[0]) for r in rows(f"SELECT quest FROM gameobject_queststarter WHERE id IN ({ints(gobjects)})")}
    reward_cols = ", ".join([f"RewardItem{i}" for i in range(1, 5)] + [f"RewardChoiceItemID{i}" for i in range(1, 7)])
    rewards = {int(v) for r in rows(f"SELECT {reward_cols} FROM quest_template WHERE ID IN ({ints(starters)})")
               for v in r if int(v) > 0}
    log(f"quest rewards: {len(rewards)} items from {len(starters)} quests")
    sourced |= rewards

    # Fishing: loot entries are zone or area ids; keep those on open maps.
    area_map = {r[0]: r[1] for r in read_dbc("AreaTable.dbc")}
    fishing = loot("fishing_loot_template")
    got = expand({e for e in fishing if area_map.get(e) in maps}, fishing, refs)
    log(f"fishing: {len(got)} items")
    sourced |= got

    # Crafted: trainer spells on open maps up to the skill cap, and allowed recipes.
    spells = {}
    for r in read_dbc("Spell.dbc"):
        effects, item_types, triggers = r[71:74], r[107:110], r[116:119]
        if 24 in effects or 36 in effects:
            spells[r[0]] = (effects, item_types, triggers)

    def created(spell_id, depth=0):
        spell = spells.get(spell_id)
        if not spell or depth > 2:
            return set()
        effects, item_types, triggers = spell
        out = set()
        for effect, item, trigger in zip(effects, item_types, triggers):
            if effect == 24 and item:
                out.add(item)
            elif effect == 36 and trigger:
                out |= created(trigger, depth + 1)
        return out

    trainer_spells = {int(r[0]) for r in rows(
        f"SELECT DISTINCT ts.SpellId FROM trainer_spell ts JOIN creature_default_trainer cdt ON cdt.TrainerId = ts.TrainerId "
        f"WHERE cdt.CreatureId IN ({ints(creatures)}) AND ts.ReqSkillRank <= {era['skill']} AND ts.ReqLevel <= {era['level']}")}
    crafted = set()
    for spell_id in trainer_spells:
        crafted |= created(spell_id)
    log(f"trainers: {len(crafted)} crafted items from {len(trainer_spells)} spells")

    template = {int(r[0]): r[1:] for r in rows(
        "SELECT entry, name, Quality, class, ItemLevel, RequiredLevel, RequiredSkillRank, spellid_1, spellid_2, DisenchantID "
        "FROM item_template")}

    def fits(entry):
        t = template.get(entry)
        return (t is not None and int(t[4]) <= era["level"] and int(t[3]) <= era["item_level"]
                and entry < era["max_entry"])

    # Containers, disenchanting and recipes build on what is already allowed: repeat until nothing new appears.
    containers, disenchant = loot("item_loot_template"), loot("disenchant_loot_template")
    allowed = {e for e in sourced | crafted if fits(e)}
    while True:
        more = expand(allowed, containers, refs)
        more |= expand({int(template[e][8]) for e in allowed if int(template[e][8])}, disenchant, refs)
        for e in allowed:
            t = template[e]
            if int(t[2]) == 9 and int(t[5]) <= era["skill"]:
                for spell_id in (int(t[6]), int(t[7])):
                    more |= created(spell_id)
        new = {e for e in more if fits(e)} - allowed
        if not new:
            break
        allowed |= new
    log(f"allowed: {len(allowed)} items (sourced {len(sourced)}, crafted {len(crafted)} before the level filters)")
    return allowed, template, era


def report(allowed, template, era_name):
    quality = collections.Counter(int(template[e][1]) for e in allowed)
    klass = collections.Counter(int(template[e][2]) for e in allowed)
    lines = [f"era {era_name}: {len(allowed)} items",
             "by quality: " + ", ".join(f"{q}:{n}" for q, n in sorted(quality.items())),
             "by class: " + ", ".join(f"{c}:{n}" for c, n in sorted(klass.items()))]
    failures = 0
    if era_name == "classic":
        for entry, why in MUST_BE_PRESENT:
            ok = entry in allowed
            failures += not ok
            lines.append(f"  {'ok  ' if ok else 'FAIL'} present  {entry} {why}")
        for entry, why in MUST_BE_ABSENT:
            ok = entry not in allowed
            failures += not ok
            lines.append(f"  {'ok  ' if ok else 'FAIL'} absent   {entry} {why}")
        for entry, why in CLOSED_UNTIL_BRACKET:
            lines.append(f"  info {'present' if entry in allowed else 'absent '}  {entry} {why} (absent until its bracket opens)")
    return "\n".join(lines), failures


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--era", default="classic", choices=sorted(ERAS))
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    allowed, template, era = build(args.era)
    text, failures = report(allowed, template, args.era)
    print(text)
    if args.dry_run:
        return 1 if failures else 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(f"# era {args.era}, {len(allowed)} items, written {time.strftime('%Y-%m-%d %H:%M')} by era_items.py\n"
                   + "\n".join(str(e) for e in sorted(allowed)) + "\n")
    tmp.replace(out)
    out.with_suffix(".report").write_text(text + "\n")
    log(f"wrote {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
