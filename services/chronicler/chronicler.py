#!/usr/bin/env python3
"""Chronicler: each faction's scribe writes up every watch of the day, and the news becomes rumours.

custom wow plans/19 (roadmap Phase 5). Reads what mod-ledger and the regard service recorded, sorts it
into reports for each faction, and has that faction's scribe write the watch into their day-book
(acore_characters.chronicle_entry). The same reports become tales (chronicle_tale): told plainly in the land
where it happened, then carried one stop along the roads each watch and garbled a little more at every stop
(round two). Word of what the other side did in contested lands and our own reaches the scribe as hearsay and
travels the same way. Where a tale is being told is a rumour row (chronicle_rumour): mod-ollama-chat (local
patch) puts one rumour for the bot's land and faction into chatter and replies; the dashboard reads
chronicle.json. Runs outside the worldserver as a user service and never touches game objects.

Standing rule: bots are people living in Azeroth. Entries and rumours are in-world words with no figures.

A watch is four hours of the local clock (midnight, dawn, morning, midday, evening, nightfall). Operator
decisions 2026-09-14: chronicle + rumours, two faction scribes, every few hours, a dashboard panel.
2026-09-15 (round two): travelling rumours and cross-faction hearsay.

Usage:
  python3 chronicler.py run [--interval 600]    service loop: write every watch that has closed, carry tales on
  python3 chronicler.py write [--watch "2026-09-14 20"] [--faction A|H] [--force] [--open] [--dry-run]
  python3 chronicler.py facts [--watch ...] [--faction A|H]   the reports only, no model calls
  python3 chronicler.py spread [--dry-run]                     carry every tale that is due one stop further
  python3 chronicler.py snapshot                               rewrite chronicle.json
  write and facts take --personal: the personal edition of each player's own characters instead of the factions;
  write --since "2026-09-14 00" writes every closed watch from then on.
Kill switch: `systemctl --user stop wow-chronicler`, or create /opt/wow/chronicler/PAUSE.
Rumours in prompts: OllamaChat.Chronicle.Rumours = 0.
"""
import argparse
import datetime as dt
import difflib
import json
import os
import random
import re
import sys
import time

# The sibling services, found from this file instead of through the /opt/wow symlinks (plan 23 W2), so a
# checkout runs wherever it is put.
_SERVICES = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(_SERVICES, "lore"))
sys.path.insert(0, os.path.join(_SERVICES, "regard"))
import era as eras  # noqa: E402
import fleet  # noqa: E402
import regard as rg  # noqa: E402  (database helpers, people, lands, company names)

# Where this realm lives comes from site/, with the live server config as the fallback (plan 23 W1).
sys.path.insert(0, _SERVICES)
from common import site  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT = os.path.join(site.get("DATA_DIR", "/opt/wow/server/data"), "dashboard-data", "chronicle.json")
PAUSE_FILE = os.path.join(HERE, "PAUSE")

# The realm's first day, for the chronicle's "day N" titles (plan 23 W9). From site.env, because a realm
# founded on another date should not be told it is living through this one's ninth day.
FIRST_DAY = dt.date.fromisoformat(site.get("REALM_FIRST_DAY", "2026-09-14"))
WATCH_HOURS = 4
WATCH_TITLE = {0: "the small hours", 4: "dawn", 8: "morning", 12: "midday", 16: "evening", 20: "nightfall"}
WATCH_SPAN = {0: "the small hours, from midnight until dawn", 4: "dawn and the early morning",
              8: "the morning", 12: "midday and the afternoon", 16: "the evening", 20: "nightfall, until midnight"}
CATCHUP_DAYS = 2              # the service writes closed watches no older than this
MIN_REPORTS = 3               # fewer and the watch is quiet: no entry is written, no model is called
MAX_REPORTS = 14              # the most a scribe is given, best first (word of the enemy included)
BUSY_LANDS = 3
MIN_TOLL = 5                  # deaths in one land before the toll is news
MIN_FALLS = 3                 # deaths to the land itself before it is worth a line
RUMOUR_REPORTS = 6            # our own news that becomes tales, per watch
HEARSAY_REPORTS = 4           # the most word of the enemy a scribe is given
HEARSAY_RUMOURS = 2           # word of the enemy that becomes tales, per watch
HEARSAY_WEIGHT = 0.7          # the enemy's deeds matter less than our own, heard second-hand
NEAR_HOURS, FAR_HOURS = 24, 12
HOP_HOURS = WATCH_HOURS       # a telling is carried one stop further once it is a watch old
MAX_HOPS = {"ours": 3, "enemy": 2}
HOP_PLACES = 4                # new places a tale reaches at each stop
RETELL_BATCH = 8
MAX_ATTEMPTS = 4
ENTRY_LANES = "zb-novelist,evo-quality,z13-qwen35"   # the novelist serves no live chat
RUMOUR_LANES = "evo-quality,z13-qwen35"               # JSON: the novelist ignores formats

SCRIBES = {
    "A": dict(key="A", name="Brother Anselm Marlowe", faction="Alliance", team=0,
              about="a scribe of the Cathedral of Light in Stormwind, who keeps the Alliance's day-book from the "
                    "reports of heralds, gryphon riders and travellers"),
    "H": dict(key="H", name="Mokra Oathkeeper", faction="Horde", team=1,
              about="a lorekeeper in Grommash Hold in Orgrimmar, who keeps the Horde's tally of deeds from the words "
                    "of wind riders, runners and wanderers"),
}
OTHER = {"A": "H", "H": "A"}
CAPITALS = {"A": [(1519, "Stormwind"), (1537, "Ironforge"), (1657, "Darnassus")],
            "H": [(1637, "Orgrimmar"), (1638, "Thunder Bluff"), (1497, "the Undercity")]}
CITIES = {zone: (name, side) for side, cities in CAPITALS.items() for zone, name in cities}

# The roads news travels (Classic): lands that share a border or a pass, and the tram, ships and zeppelins.
ROADS = [
    # Eastern Kingdoms
    (1519, 12), (12, 40), (12, 10), (12, 44), (40, 10), (10, 44), (10, 33), (10, 8),   # Duskwood to the Swamp by Deadwind Pass
    (8, 4), (44, 46), (46, 51), (51, 3), (51, 38), (3, 38), (38, 1), (1, 1537), (38, 11),
    (11, 45), (45, 267), (45, 47), (267, 47), (267, 36), (267, 130), (36, 130), (36, 28),
    (130, 85), (85, 1497), (85, 28), (28, 139), (28, 47), (47, 139),
    # Kalimdor
    (1657, 141), (141, 148), (148, 331), (331, 361), (361, 618), (331, 16), (331, 17), (331, 406),
    (1637, 14), (14, 17), (17, 215), (215, 1638), (17, 406), (406, 405), (405, 357), (357, 400),
    (17, 400), (17, 15), (400, 440), (440, 490), (490, 1377),
    # Over the water: the tram, Menethil's ships, Booty Bay to Ratchet, the zeppelins
    (1519, 1537), (11, 148), (11, 15), (33, 17), (1637, 1497), (1637, 33), (1497, 33),
]
NEIGHBOURS = {}
for _a, _b in ROADS:
    NEIGHBOURS.setdefault(_a, set()).add(_b)
    NEIGHBOURS.setdefault(_b, set()).add(_a)

KILL_WORDS = {1: "a fearsome foe", 2: "a rare and fearsome creature", 3: "a great terror of the land",
              4: "a rare creature seldom seen"}
KILL_WEIGHT = {1: 3, 2: 6, 3: 10, 4: 4}
STRONGHOLD_WEIGHT = 14        # a delve cleared outranks anything else a watch can hold
INSTANCE_PLACES = set(rg.lands.INSTANCE_NAMES.values())
# mod-ledger writes `"boss":1`. MySQL's JSON comparison keeps integer 1 and boolean true apart, so
# `JSON_EXTRACT(detail, '$.boss') = true` matches nothing at all -- it only ever looked right here because
# every boss killed so far was also elite and came in on the rank clause beside it (plans/30 §6).
BOSS_SQL = "JSON_EXTRACT(detail, '$.boss') = 1"


def is_boss(v):
    """mod-ledger writes the flag as `"boss":1`, not as a JSON true (mod_ledger_scripts.cpp:119), and both
    readers here tested it as a boolean. Every dungeon boss on the realm therefore scored as an ordinary
    elite (weight 3 instead of 8) and lost its place to trash: Edwin VanCleef was cut from the watch that
    held him by the MAX_REPORTS cap, outranked by a Goblin Engineer (plans/30 §6). Accept either shape."""
    return v in (1, True, "1", "true", "True")
MILESTONE = {10: "is a raw recruit no longer", 20: "has been blooded and proven", 30: "has become a tested fighter",
             40: "is counted among the seasoned", 50: "is now a veteran whose name carries weight",
             60: "stands among the most seasoned fighters of the age"}
INCIDENT_WEIGHT = {"taken": 7, "contested": 5, "held": 4, "same_prey": 4, "released": 3, "duel": 3, "words": 3}

ORDINAL = ["", "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
           "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth",
           "nineteenth"]
TENS = {20: "twentieth", 30: "thirtieth", 40: "fortieth", 50: "fiftieth", 60: "sixtieth", 70: "seventieth",
        80: "eightieth", 90: "ninetieth"}
TENS_CARDINAL = {20: "twenty", 30: "thirty", 40: "forty", 50: "fifty", 60: "sixty", 70: "seventy", 80: "eighty",
                 90: "ninety"}
DIGITS = re.compile(r"\d")

log = rg.log
sql, q = rg.sql, rg.q


def ordinal(n):
    if n < 20:
        return ORDINAL[n]
    if n < 100:
        tens, ones = n // 10 * 10, n % 10
        return TENS[tens] if not ones else f"{TENS_CARDINAL[tens]}-{ORDINAL[ones]}"
    return "latest"


def count_words(n, one, many):
    """In-world amounts: no figures reach a scribe or a rumour."""
    if n == 1:
        return "one " + one
    phrase = {2: "two", 3: "three"}.get(n) or (
        "a handful of" if n <= 6 else "a dozen or so" if n <= 15 else "a few score" if n <= 40 else
        "many dozens of" if n <= 99 else "hundreds of" if n <= 499 else "a great many")
    return f"{phrase} {many}".replace(" of of ", " of ")


def cap(s):
    return s[:1].upper() + s[1:]


def land_title(zone):
    return cap(rg.land_name(zone))


def place_name(zone):
    return CITIES[zone][0] if zone in CITIES else rg.LAND[zone][1] if zone in rg.LAND else ""


def side_of(zone):
    return CITIES[zone][1] if zone in CITIES else rg.LAND[zone][4] if zone in rg.LAND else ""


# ---------------------------------------------------------------------------
# watches

def watch_start(t):
    return t.replace(minute=0, second=0, microsecond=0, hour=t.hour - t.hour % WATCH_HOURS)


def title_of(start):
    day = (start.date() - FIRST_DAY).days + 1
    return f"The {ordinal(max(1, day))} day, {WATCH_TITLE[start.hour]}"


def stamp(t):
    return t.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# reports: what reached the scribe during the watch

class Reports:
    def __init__(self):
        self.items = []    # dict(text, weight, land or 0, rumour: whether folk would pass it on, enemy: hearsay)
        self.names = set()  # people, companies and foes named, for the judge

    def add(self, text, weight, land=0, rumour=True):
        self.items.append(dict(text=text, weight=float(weight), land=int(land or 0), rumour=rumour, enemy=False))

    def best(self, n=MAX_REPORTS):
        return sorted(self.items, key=lambda r: -r["weight"])[:n]


def faction_of_race(race):
    return "H" if int(race) in rg.HORDE else "A"


def describe(p, names, stranger=False):
    """A stranger is seen, not known: the other side's scouts give a race, a calling and a banner, never a name."""
    race, calling = rg.RACES.get(p["race"], "wanderer"), rg.CLASSES.get(p["cls"], "traveller")
    company = f" of {names[p['guild']]}" if p["guild"] in names else ""
    if stranger:
        return f"{'an' if race[:1] in 'AEIOU' else 'a'} {race} {calling}{company}"
    return f"{p['name']} the {race} {calling}{company}"


def who(guids, people, names, stranger=False):
    ps = [people[g] for g in guids if g in people]
    parts = [describe(p, names, stranger) for p in ps[:3]]
    extra = len(ps) - 3
    if extra > 0:
        parts.append(count_words(extra, "other", "others") if stranger else count_words(extra, "companion", "companions"))
    return rg.join_names(parts, cap=len(parts)) if parts else "some of our people"


def grouped(rows):
    """rows: (ts, key, guid). The same foe in the same land is one deed for the watch, however often it came."""
    deeds = {}
    for ts, key, guid in sorted(rows):
        d = deeds.setdefault(key, dict(key=key, guids=[], times=0))
        d["times"] += 1
        if guid not in d["guids"]:
            d["guids"].append(guid)
    return list(deeds.values())


def foe(name, many):
    """A foe with many of its kind about is one of many ("a Flesh Golem"); one alone has a name ("Old Icebeard")."""
    if not many or name.lower().startswith("the "):
        return name
    return ("an " if name[:1] in "AEIOU" else "a ") + name


def again(deed):
    return " more than once" if deed["times"] > 1 else ""


def stood_with(parties, striker, ts):
    """The company standing when the blow landed, the one who struck it first: who() names only the first
    three, and a watch about the main that opens with someone else's name is not about them."""
    others = parties.standing_with(striker, ts) - {striker}
    return [striker] + sorted(others)


def find_delves(window, parties):
    """Every stronghold entered in the watch: where, which of its masters fell, and who stood there.
    Read straight from the kill log rather than from one house's or one faction's credits, because a boss
    leaves a kill row for the single guid who landed the blow -- and the watch should say the place was
    cleared, not that one dwarf got the last hit on three of seven masters."""
    delves = {}
    for r in sql("SELECT actor_guid, zone_id, map_id, UNIX_TIMESTAMP(ts), "
                 "JSON_UNQUOTE(JSON_EXTRACT(detail, '$.name')) FROM ledger_event e "
                 f"WHERE event_type = 'kill' AND {window} AND {BOSS_SQL} "
                 "ORDER BY id"):
        g, zone, map_id, ts = int(r[0]), int(r[1]), int(r[2]), float(r[3])
        place, where = rg.deed_place(zone, map_id), rg.land_of(zone, map_id)
        if place not in INSTANCE_PLACES or not where:
            continue
        d = delves.setdefault(place, dict(place=place, where=where, names=[], guids=[]))
        name = "\t".join(r[4:])
        if name and name not in d["names"]:
            d["names"].append(name)
        for x in stood_with(parties, g, ts):
            if x not in d["guids"]:
                d["guids"].append(x)
    return list(delves.values())


def keep_stronghold(strongholds, place, where, names_, guids):
    """Hold the fallen masters aside until the whole delve can be told as one deed."""
    sh = strongholds.setdefault(place, dict(where=where, names=[], guids=[]))
    for name in ([names_] if isinstance(names_, str) else names_):
        if name and name not in sh["names"]:
            sh["names"].append(name)
    for g in guids:
        if g not in sh["guids"]:
            sh["guids"].append(g)


def tell_strongholds(reports, strongholds, people, names, stranger=False):
    """Going into a stronghold and coming out again is the largest thing that happens on a quiet realm, and
    until now no scribe could say it at all: the deeds inside were reported as ordinary kills in the land
    above (plans/30 §6). Told as one deed, at a weight nothing else in a watch reaches."""
    for place, sh in strongholds.items():
        reports.names.add(place)
        reports.names.update(sh["names"])
        fallen = (f"its master, {sh['names'][0]}" if len(sh["names"]) == 1
                  else f"its masters: {rg.join_names(sh['names'], cap=len(sh['names']))}")
        reports.add(f"{cap(who(sh['guids'], people, names, stranger))} went into {place} and brought down "
                    f"{fallen}.", STRONGHOLD_WEIGHT + len(sh["guids"]) - 1, sh["where"])


def gather(start, end, faction, heard_by=None):
    """The reports of `faction`'s people for the watch. With heard_by (the other faction), only what that side's
    scouts could see: deeds, falls and company clashes in contested lands and their own, the doers nameless."""
    stranger = heard_by is not None
    names = rg.company_names()
    window = f"e.ts >= {q(stamp(start))} AND e.ts < {q(stamp(end))}"
    reports = Reports()
    reports.names.update(names.values())

    kills = sql("SELECT e.actor_guid, e.zone_id, e.map_id, UNIX_TIMESTAMP(e.ts), JSON_EXTRACT(e.detail, '$.entry'), "
                "COALESCE(JSON_EXTRACT(e.detail, '$.rank'), 0), COALESCE(JSON_EXTRACT(e.detail, '$.boss'), 'false'), "
                "JSON_UNQUOTE(JSON_EXTRACT(e.detail, '$.name')) FROM ledger_event e "
                f"WHERE e.event_type = 'kill' AND {window} AND e.detail LIKE '{{%' "
                f"AND (JSON_EXTRACT(e.detail, '$.rank') > 0 OR {BOSS_SQL.replace('detail', 'e.detail')})")
    deaths = sql("SELECT e.actor_guid, e.zone_id, e.map_id, UNIX_TIMESTAMP(e.ts), ct.entry, ct.`rank`, ct.name "
                 "FROM ledger_event e JOIN acore_world.creature_template ct ON ct.entry = JSON_EXTRACT(e.detail, '$.entry') "
                 f"WHERE e.event_type = 'death' AND {window} AND ct.`rank` > 0")
    tallies = sql("SELECT e.actor_guid, e.zone_id, e.map_id, e.event_type, COUNT(*) FROM ledger_event e "
                  f"WHERE e.event_type IN ('kill', 'death', 'quest_complete') AND {window} GROUP BY 1, 2, 3, 4")
    quests = [] if stranger else sql(
        "SELECT e.actor_guid, e.zone_id, e.map_id, JSON_UNQUOTE(JSON_EXTRACT(e.detail, '$.title')) "
        f"FROM ledger_event e WHERE e.event_type = 'quest_complete' AND {window}")
    levels = [] if stranger else sql(
        "SELECT e.actor_guid, e.zone_id, e.map_id, JSON_EXTRACT(e.detail, '$.new') FROM ledger_event e "
        f"WHERE e.event_type = 'level_up' AND {window} AND JSON_EXTRACT(e.detail, '$.new') % 10 = 0")
    moments = [] if stranger else sql(
        "SELECT l.bot_guid, l.other_guid, l.delta, l.reason FROM regard_log l "
        f"WHERE l.source = 'chat' AND l.ts >= {q(stamp(start))} AND l.ts < {q(stamp(end))} AND ABS(l.delta) >= 5")
    incidents = sql("SELECT i.guild_a, COALESCE(i.guild_b, 0), COALESCE(i.zone_id, 0), i.kind, i.detail FROM guild_incident i "
                    f"WHERE i.ts >= {q(stamp(start))} AND i.ts < {q(stamp(end))} ORDER BY i.id")

    # Who stood with the one who struck. A boss leaves a kill row for a single guid, so without this the
    # whole company that went in is missing from the telling (plans/30 §6).
    parties = rg.Parties(end)
    delves = find_delves(window, parties)

    guids = [r[0] for r in kills + deaths + tallies + quests + levels] + [g for m in moments for g in m[:2]] \
        + [g for d in delves for g in d["guids"]]
    people = rg.people(guids)
    ours = {g for g, p in people.items() if faction_of_race(p["race"]) == faction}
    if not stranger:
        reports.names.update(people[g]["name"] for g in ours)
    land = lambda r: rg.land_of(int(r[1]), int(r[2]))
    entries = sorted({int(float(r[4])) for r in kills + deaths if r[4] not in ("", "NULL")})
    spawns = {int(r[0]): int(r[1]) for r in sql(
        f"SELECT id, COUNT(*) FROM acore_world.creature WHERE id IN ({','.join(map(str, entries))}) GROUP BY id")
        } if entries else {}
    many = lambda entry: spawns.get(int(float(entry)), 1) > 1

    # Rare and fearsome foes felled.
    rows = []
    for r in kills:
        g, where = int(r[0]), land(r)
        if g in ours and where:
            rows.append((float(r[3]), (r[4], where, int(r[5]), is_boss(r[6]),
                                       rg.deed_place(int(r[1]), int(r[2])), "\t".join(r[7:])), g))
    strongholds = {}
    for d in delves:
        ours_there = [g for g in d["guids"] if g in ours]
        if ours_there:
            keep_stronghold(strongholds, d["place"], d["where"], d["names"], ours_there)
    for d in grouped(rows):
        entry, where, rank, boss, place, name = d["key"]
        reports.names.add(name)
        reports.names.add(place)
        # Masters of a stronghold are held back and told as one deed below: going in and coming out again
        # is the thing that happened, and three separate kill lines both bury it and crowd the watch.
        if boss and place in INSTANCE_PLACES:
            continue        # told as one delve above
        if boss:
            text = (f"{cap(who(d['guids'], people, names, stranger))} slew {foe(name, many(entry))}, the master of "
                    f"{place}{again(d)}.")
        else:
            text = (f"{cap(who(d['guids'], people, names, stranger))} slew {foe(name, many(entry))}, "
                    f"{KILL_WORDS.get(rank, 'a fearsome foe')}, in {place}{again(d)}.")
        reports.add(text, (8 if boss else KILL_WEIGHT.get(rank, 3)) + len(d["guids"]) - 1, where)
    tell_strongholds(reports, strongholds, people, names, stranger)

    # Our people struck down by such foes.
    rows = []
    for r in deaths:
        g, where = int(r[0]), land(r)
        if g in ours and where:
            rows.append((float(r[3]), (r[4], where, int(r[5]), rg.deed_place(int(r[1]), int(r[2])), r[6]), g))
    for d in grouped(rows):
        entry, where, rank, place, name = d["key"]
        reports.names.add(name)
        reports.names.add(place)
        one = len(d["guids"]) == 1
        reports.add(f"{cap(who(d['guids'], people, names, stranger))} {'was' if one else 'were'} struck down by "
                    f"{foe(name, many(entry))} in {place}{again(d)}, but {'lives' if one else 'live'} to fight "
                    "again.", 3 + KILL_WEIGHT.get(rank, 3) / 2, where)

    # Lands that kill without an enemy. These never reached a scribe before: the
    # struck-down tale above joins on a creature, and a fall has none, so every
    # drowning and every cliff in the world was invisible to the chronicle.
    falls = sql("SELECT e.actor_guid, e.zone_id, e.map_id FROM ledger_event e "
                f"WHERE e.event_type = 'death' AND {window} "
                "AND JSON_UNQUOTE(JSON_EXTRACT(e.detail, '$.cause')) = 'environment'")
    per_fall = {}
    for r in falls:
        g, where = int(r[0]), land(r)
        if g in ours and where:
            per_fall[where] = per_fall.get(where, 0) + 1
    for where, n in sorted(per_fall.items(), key=lambda kv: -kv[1])[:2]:
        if n >= MIN_FALLS:
            whom = "of their people" if stranger else "of our people"
            reports.add(f"{land_title(where)} needs no enemy: {count_words(n, whom, whom)} fell to the rocks or "
                        "the water there, with no hand upon them.", 2.5, where)

    # The busiest lands, and lands with a heavy toll.
    per_land = {}
    for r in tallies:
        g, where = int(r[0]), land(r)
        if g not in ours or not where:
            continue
        t = per_land.setdefault(where, dict(people=set(), kill=0, death=0, quest_complete=0))
        t["people"].add(g)
        t[r[3]] += int(r[4])
    busiest = [] if stranger else sorted(per_land.items(), key=lambda kv: -(len(kv[1]["people"]) * 10 + kv[1]["kill"]))
    for where, t in busiest[:BUSY_LANDS]:
        parts = [f"{count_words(len(t['people']), 'of our people was', 'of our people were')} abroad"]
        if t["kill"]:
            parts.append(f"{count_words(t['kill'], 'foe or beast', 'foes and beasts')} fell to them")
        if t["death"]:
            parts.append(f"{count_words(t['death'], 'of them was', 'of them were')} struck down")
        if t["quest_complete"]:
            parts.append(f"{count_words(t['quest_complete'], 'task was', 'tasks were')} seen through for the folk there")
        reports.add(f"In {rg.land_name(where)}, " + ", ".join(parts) + ".", 2.5, where, rumour=False)
    for where, t in per_land.items():
        if t["death"] >= MIN_TOLL and where not in dict(busiest[:BUSY_LANDS]):
            whom = ("of their people was", "of their people were") if stranger else ("of our people was", "of our people were")
            reports.add(f"{land_title(where)} was cruel: {count_words(t['death'], *whom)} struck down there.", 2, where)

    # A few of the tasks seen through, by name.
    done = {}
    for r in quests:
        g, where, title = int(r[0]), land(r), "\t".join(r[3:]).strip()
        if g in ours and where and title and title != "NULL" and not DIGITS.search(title):
            gs = done.setdefault((title, where), [])
            if g not in gs:
                gs.append(g)
    picks = sorted(done.items(), key=lambda kv: (-len(kv[1]), random.random()))[:3]
    for (title, where), gs in picks:
        reports.add(f"{cap(who(gs, people, names))} saw through the task known as \"{title}\" in {rg.land_name(where)}.",
                    1.5 + len(gs) / 2, where, rumour=False)

    # People coming into their strength.
    for r in levels:
        g, new = int(r[0]), int(float(r[3]))
        if g in ours and new in MILESTONE:
            reports.add(f"{describe(people[g], names)} {MILESTONE[new]}.", 2 + new / 20, land(r))

    # Words that mattered between people.
    seen = set()
    for bot, other, delta, reason in sorted(moments, key=lambda m: -abs(float(m[2])))[:3]:
        a, b = people.get(int(bot)), people.get(int(other))
        if not a or not b or int(bot) not in ours or reason in seen:
            continue
        seen.add(reason)
        reports.names.add(b["name"])
        # The reason is the feeler's own memory ("they mocked your candlelight": you are a, they are b).
        reports.add(f"Words passed between {a['name']} and {b['name']}; as {a['name']} remembers it: {reason}",
                    2 + abs(float(delta)) / 5)

    # The companies: claims, lands taken and contested, clashes.
    company_side = {int(r[0]): faction_of_race(r[1]) for r in sql(
        "SELECT g.guildid, c.race FROM guild g JOIN characters c ON c.guid = g.leaderguid")}
    claims = []
    for a, b, zone, kind, detail in incidents:
        a, b, zone = int(a), int(b), int(zone)
        sides = (company_side.get(a), company_side.get(b))
        if faction not in sides or (stranger and heard_by in sides):   # the hearer's own gather tells a clash with them
            continue
        if kind == "claimed":
            claims.append((zone, detail))
        else:
            reports.add(detail.rstrip(".") + ".", INCIDENT_WEIGHT.get(kind, 3), zone if zone in rg.LAND else 0)
    if len(claims) > 3 and not stranger:
        reports.add("The companies staked their claims across the lands: "
                    + "; ".join(d for _, d in claims[:6]) + (", and more besides." if len(claims) > 6 else "."), 5)
    else:
        for zone, detail in claims:
            reports.add(detail.rstrip(".") + ".", 4, zone if zone in rg.LAND else 0)

    if stranger:
        reports.items = [dict(r, enemy=True, weight=r["weight"] * HEARSAY_WEIGHT) for r in reports.items
                         if r["land"] in rg.LAND and side_of(r["land"]) in ("C", heard_by)]
    return reports


def watch_reports(start, end, faction):
    """Our own reports and the word of the enemy, best first, and every name the judge should let pass."""
    ours = gather(start, end, faction)
    heard = gather(start, end, OTHER[faction], heard_by=faction)
    enemy = heard.best(HEARSAY_REPORTS)
    return ours.best(MAX_REPORTS - len(enemy)) + enemy, ours.names | heard.names


# ---------------------------------------------------------------------------
# writing

def lanes_from(names):
    return fleet.prefer_lanes(names)


def attempt_on(lanes, attempt):
    usable = [lane for lane in lanes if lane.available()] or lanes
    return usable[(attempt - 1) % len(usable)]


def check_words(text, e):
    if e["meta"].search(text):
        raise ValueError(f"out-of-world word: {e['meta'].search(text).group(0)!r}")
    if DIGITS.search(text):
        raise ValueError("figures in the text")


def json_reply(out):
    m = re.search(r"\{.*\}", out, re.S)
    return json.loads(m.group(0)) if m else {}


def entry_prompt(scribe, e, start, reports):
    enemy = SCRIBES[OTHER[scribe["key"]]]["faction"]
    lines = "\n".join(f"- {r['text']}" for r in reports if not r["enemy"]) or "- (nothing from our own people)"
    heard = "\n".join(f"- {r['text']}" for r in reports if r["enemy"])
    return (f"You are {scribe['name']}, {scribe['about']}. The world you live in: {e['setting']}\n\n"
            f"Write the entry in your day-book for {WATCH_SPAN[start.hour]}. These reports reached you:\n{lines}\n\n"
            + (f"Word of the {enemy}, carried by scouts and travellers (none of our own saw it):\n{heard}\n\n" if heard else "")
            + f"Write it as {scribe['name']} would, in plain prose of about 150 to 250 words, one to three paragraphs. "
            f"Name the people, companies and places given, weigh what matters most to the {scribe['faction']}, and let "
            "your own voice and your people's view colour it. "
            + (f"Tell the word of the {enemy} as hearsay, with the fear, scorn or grudging respect your people feel; "
               "those people stay nameless. " if heard else "")
            + "Use only these reports: invent no other deeds, deaths, people or places. Those struck down were wounded, "
            "not slain; everyone named lives. No figures or digits (say \"a handful\", \"scores\"), no title, headings, "
            "lists or markdown.")


def clean_entry(text, title):
    lines = [ln for ln in text.strip().strip('"').splitlines()
             if not ln.lstrip().startswith(("#", "**", "Title:")) and ln.strip().lower() != title.lower()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def write_entry(scribe, e, start, reports, known, lanes, judge):
    return write_prose(entry_prompt(scribe, e, start, reports), title_of(start), e, known, lanes, judge, 80)


def write_prose(prompt, title, e, known, lanes, judge, min_words):
    messages = [{"role": "user", "content": prompt}]
    errors = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        lane = attempt_on(lanes, attempt)
        try:
            body = clean_entry(lane.chat(messages, 700, temperature=0.85), title)
            check_words(body, e)
            if len(body.split()) < min_words:
                raise ValueError("too short")
            flag = judge.check(eras.judge_prompt(e, body, known))
            if flag and attempt < MAX_ATTEMPTS:
                raise ValueError(f"judge: {flag}")
            return title, body, lane.model, flag
        except Exception as ex:
            errors.append(f"{lane.name}: {ex}")
            log(f"entry attempt {attempt} on {lane.name} failed: {ex}")
    raise RuntimeError("; ".join(errors))


def rumour_prompt(scribe, e, start, reports):
    enemy = SCRIBES[OTHER[scribe["key"]]]["faction"]
    lines = "\n".join(f"{i}. {'(word of the ' + enemy + ') ' if r['enemy'] else ''}{r['text']}"
                      for i, r in enumerate(reports, 1))
    return (f"You help news travel in Azeroth. The world: {e['setting']}\n\n"
            f"These things were reported among the {scribe['faction']} during {WATCH_SPAN[start.hour]}:\n{lines}\n\n"
            f"Pick up to {RUMOUR_REPORTS} of our own people's reports that ordinary folk would talk about most. For each, "
            "write two things people would say aloud:\n"
            "- near: how someone in the land where it happened passes it on. Only what the report says, with the same "
            "names, people and places: add no description of the foe, no aftermath and nothing else. Under 20 words.\n"
            "- far: the same news after many mouths, told in some other land as hearsay. Vary how it is attributed "
            "and do not open them all alike (\"a runner swore\", \"the ferryman had it from\", \"a pedlar tells it\", "
            "\"word reached us that\", \"they say\", or no attribution at all). Change exactly one detail (a name "
            "half-remembered, the foe grown larger, more heroes than there "
            "were) and invent nothing else. Under 25 words.\n"
            f"Also pick up to {HEARSAY_RUMOURS} marked as word of the {enemy}, if any would be talked about. For each, "
            f"write only far (leave near empty): how {scribe['faction']} folk in that land pass on what the {enemy} did, "
            "as hearsay coloured by fear, scorn or grudging respect. The doers stay nameless as in the report; change at "
            "most one detail and invent nothing else. Under 25 words.\n"
            "No figures or digits and no game words. Those struck down were wounded, not slain.\n"
            'Reply with JSON only: {"rumours": [{"report": <its number>, "near": "...", "far": "..."}]}')


def write_rumours(scribe, e, start, reports, known, lanes, judge):
    """Tales for the watch: dict(kind, origin, near or '', far). Deeds, falls, clashes and people rising travel;
    tallies and errands only go in the day-book."""
    placed = [r for r in reports if r["land"] in rg.LAND and r["rumour"]]
    if not placed:
        return []
    messages = [{"role": "user", "content": rumour_prompt(scribe, e, start, placed)}]
    errors = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        lane = attempt_on(lanes, attempt)
        try:
            items = json_reply(lane.chat(messages, 900, temperature=0.6, json_mode=True)).get("rumours", [])
            kept, used = [], set()
            for it in items:
                try:
                    n = int(it["report"])
                    report = placed[n - 1]
                    near, far = str(it.get("near") or "").strip().strip('"'), str(it["far"]).strip().strip('"')
                    check_words(near + " " + far, e)
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
                kind = "enemy" if report["enemy"] else "ours"
                if kind == "enemy":
                    near = ""
                if n in used or not far or (kind == "ours" and not near) or len(near) > 255 or len(far) > 255 \
                        or sum(t["kind"] == kind for t in kept) >= (HEARSAY_RUMOURS if kind == "enemy" else RUMOUR_REPORTS):
                    continue
                used.add(n)
                kept.append(dict(kind=kind, origin=report["land"], near=near, far=far))
            if not kept:
                raise ValueError("no usable rumours")
            flag = judge.check(eras.judge_prompt(e, "\n".join(f"{t['near']}\n{t['far']}" for t in kept), known))
            if flag:
                if attempt < MAX_ATTEMPTS:
                    raise ValueError(f"judge: {flag}")
                log(f"rumours still flagged on the last attempt ({flag}); none kept")
                return []
            return kept
        except Exception as ex:
            errors.append(f"{lane.name}: {ex}")
            log(f"rumour attempt {attempt} on {lane.name} failed: {ex}")
    log("rumours failed: " + "; ".join(errors))
    return []


# ---------------------------------------------------------------------------
# travelling tales

def next_places(here, reached, origin, faction):
    """Where a telling is carried next: along the roads from the places it is told now, to places the faction's
    people go that it has not reached, cities first, then the lands nearest in danger to where it happened."""
    centre = rg.LAND[origin][2] + rg.LAND[origin][3]
    onward = {n for z in here for n in NEIGHBOURS.get(z, ())} - set(reached) - set(here)
    onward = [z for z in onward if side_of(z) in (faction, "C")]
    onward.sort(key=lambda z: (z not in CITIES,
                               abs(rg.LAND[z][2] + rg.LAND[z][3] - centre) if z in rg.LAND else 0, random.random()))
    return onward[:HOP_PLACES]


COMMON = {"a", "an", "the", "and", "but", "or", "i", "we", "you", "they", "he", "she", "it", "some", "someone",
          "word", "scouts", "folk", "one", "two", "three", "aye", "no", "yes", "now", "then", "there", "that", "this",
          "who", "what", "heard", "say", "says", "said", "swore", "by", "in", "on", "at", "of", "with", "from",
          "light", "alliance", "horde", "scourge"}
PLACE_WORDS = {w.lower() for z in list(rg.LAND) + list(CITIES) for w in re.findall(r"[\w'-]+", place_name(z))} \
    | {w.lower() for n in rg.lands.INSTANCE_NAMES.values() for w in re.findall(r"[\w'-]+", n)} \
    | {w.lower() for r in rg.RACES.values() for w in r.split()} | {"elves", "dwarves", "orcs", "trolls", "gnomes", "humans"}


def names_in(text):
    """Capitalised words that do not open a sentence, a clause after a colon, or a quote."""
    out = set()
    for part in re.split(r"[.!?;:\"“”]+", text):
        words = re.findall(r"[A-Za-z][\w'-]*", part)
        out.update(w for w in words[1:] if w[0].isupper() and w.lower() not in COMMON)
    return out


def new_names(child, parent):
    """Names in a retelling that are neither in the telling it came from, half-remembered from one, nor a place."""
    known = {w.lower() for w in re.findall(r"[\w'-]+", parent)} | PLACE_WORDS
    theirs = [w.lower() for w in names_in(parent)]
    return [w for w in names_in(child) if w.lower() not in known
            and not any(difflib.SequenceMatcher(None, w.lower(), t).ratio() >= 0.6 for t in theirs)]


def retell_prompt(scribe, e, jobs):
    enemy = SCRIBES[OTHER[scribe["key"]]]["faction"]
    lines = "\n".join(
        f"{i}. {'(word of the ' + enemy + ') ' if j['kind'] == 'enemy' else ''}told in "
        f"{rg.join_names([place_name(z) for z in j['here']], cap=9)}: \"{j['words']}\" "
        f"-> carried on to {rg.join_names([place_name(z) for z in j['onward']], cap=9)}"
        for i, j in enumerate(jobs, 1))
    return (f"You help news travel in Azeroth. The world: {e['setting']}\n\n"
            f"Word going around among the {scribe['faction']}, and where travellers carry it next:\n{lines}\n\n"
            "For each, write how it sounds one stop further along the road, after a few more mouths, as hearsay. "
            "Vary how it is attributed and do not begin them all the same way (\"a carter swore\", \"I had it from a "
            "fisherman\", \"the miller's boy tells it\", \"word came down the river\", \"a pedlar out of the hills has "
            "it\", \"they say\", or no attribution at all). Keep the heart of it and change exactly "
            "one more detail: a name half-remembered or lost, the foe grown larger or stranger, more or fewer people, the "
            "place blurred (\"somewhere up north\"). Name no person, company or place that is not already in it. "
            "Word of the enemy keeps its fear or scorn. Under 25 words, no figures or digits and no game words.\n"
            'Reply with JSON only: {"tales": [{"n": <its number>, "words": "..."}]}')


def retell(scribe, e, jobs, lanes, judge):
    """{tale id: the retelling}. A telling the model botches is left out (its road ends); raises when no lane
    answered, so the tales wait for the next cycle."""
    messages = [{"role": "user", "content": retell_prompt(scribe, e, jobs)}]
    known = set().union(*(names_in(j["words"]) for j in jobs))
    errors = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        lane = attempt_on(lanes, attempt)
        try:
            items = json_reply(lane.chat(messages, 900, temperature=0.7, json_mode=True)).get("tales", [])
            kept = {}
            for it in items:
                try:
                    job = jobs[int(it["n"]) - 1]
                    words = str(it["words"]).strip().strip('"')
                    check_words(words, e)
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
                if words and len(words) <= 255 and words != job["words"] and not new_names(words, job["words"]):
                    kept[job["id"]] = words
            if not kept:
                raise ValueError("no usable retellings")
            flag = judge.check(eras.judge_prompt(e, "\n".join(kept.values()), known))
            if flag:
                if attempt < MAX_ATTEMPTS:
                    raise ValueError(f"judge: {flag}")
                log(f"retellings still flagged on the last attempt ({flag}); those roads end")
                return {}
            return kept
        except Exception as ex:
            errors.append(f"{lane.name}: {ex}")
            log(f"retelling attempt {attempt} on {lane.name} failed: {ex}")
    raise RuntimeError("; ".join(errors))


def tale_sql(entry, root, parent, team, kind, hop, origin, words, told, spread):
    return ("INSERT INTO chronicle_tale (entry_id, root_id, parent_id, team, kind, hop, origin, words, told_at, spread_at) "
            f"VALUES ({entry}, {root}, {parent}, {team}, {q(kind)}, {hop}, {origin}, {q(words)}, {q(told)}, "
            f"{q(spread) if spread else 'NULL'});")


def places_sql(entry, tale, zones, team, hop, words, told, hours):
    if not zones:
        return ""
    return ("INSERT INTO chronicle_rumour (entry_id, tale_id, zone_id, team, distance, words, expires_at) VALUES "
            + ",".join(f"({entry}, {tale}, {z}, {team}, {hop}, {q(words)}, {q(told)} + INTERVAL {hours} HOUR)" for z in zones)
            + ";")


def spread(args, judge):
    """Every telling a watch old is carried one stop further along the roads, garbled a little more; a telling
    whose word has died out, whose road ends or that has gone as far as it goes stays where it is."""
    now = dt.datetime.now()
    rows = sql("SELECT t.id, COALESCE(t.root_id, t.id), t.entry_id, t.team, t.kind, t.hop, t.origin, HEX(t.words), "
               "UNIX_TIMESTAMP(t.told_at), COALESCE(GROUP_CONCAT(r.zone_id), '') FROM chronicle_tale t "
               "LEFT JOIN chronicle_rumour r ON r.tale_id = t.id "
               f"WHERE t.spread_at IS NULL AND t.told_at <= {q(stamp(now - dt.timedelta(hours=HOP_HOURS)))} "
               "GROUP BY t.id ORDER BY t.id")
    if not rows:
        return 0
    reached = {}
    for root, zone in sql("SELECT DISTINCT COALESCE(t.root_id, t.id), r.zone_id FROM chronicle_tale t "
                          "JOIN chronicle_rumour r ON r.tale_id = t.id "
                          f"WHERE COALESCE(t.root_id, t.id) IN ({','.join(sorted({r[1] for r in rows}))})"):
        reached.setdefault(root, set()).add(int(zone))
    ended, jobs = [], {}
    for tid, root, entry, team, kind, hop, origin, words, told, places in rows:
        hop, origin, faction = int(hop), int(origin), "A" if int(team) == 0 else "H"
        here = sorted({int(z) for z in places.split(",") if z not in ("", "NULL")})
        alive = float(told) + FAR_HOURS * 3600 > time.time()
        onward = next_places(here, reached.get(root, set()), origin, faction) \
            if alive and here and hop < MAX_HOPS.get(kind, 0) and origin in rg.LAND else []
        if not onward:
            ended.append(tid)
            continue
        jobs.setdefault(faction, []).append(dict(id=tid, root=root, entry=entry, team=int(team), kind=kind, hop=hop,
                                                 origin=origin, words=bytes.fromhex(words).decode(), here=here,
                                                 onward=onward))
    e, told = eras.get(args.era), stamp(now)
    stmts, carried = [], 0
    for faction, todo in jobs.items():
        scribe = SCRIBES[faction]
        for i in range(0, len(todo), RETELL_BATCH):
            batch = todo[i:i + RETELL_BATCH]
            try:
                words = retell(scribe, e, batch, lanes_from(args.rumour_lanes), judge)
            except Exception as ex:  # every lane down: the batch waits for the next cycle
                log(f"retelling waits ({scribe['faction']}): {type(ex).__name__}: {ex}")
                continue
            for j in batch:
                if j["id"] not in words:
                    ended.append(j["id"])
                    continue
                carried += 1
                if args.dry_run:
                    print(f"  [{scribe['faction']} {j['kind']} hop {j['hop']}→{j['hop'] + 1}] "
                          f"{', '.join(place_name(z) for z in j['onward'])}\n    was: {j['words']}\n    now: {words[j['id']]}")
                    continue
                stmts += [tale_sql(j["entry"], j["root"], j["id"], j["team"], j["kind"], j["hop"] + 1, j["origin"],
                                   words[j["id"]], told, None),
                          "SET @tale = LAST_INSERT_ID();",
                          places_sql(j["entry"], "@tale", j["onward"], j["team"], j["hop"] + 1, words[j["id"]], told,
                                     FAR_HOURS),
                          f"UPDATE chronicle_tale SET spread_at = {q(told)} WHERE id = {j['id']};"]
    if args.dry_run:
        print(f"{carried} carried on, {len(ended)} roads end")
        return 0
    if ended:
        stmts.append(f"UPDATE chronicle_tale SET spread_at = {q(told)} WHERE id IN ({','.join(map(str, ended))});")
    if stmts:
        sql("\n".join(["START TRANSACTION;"] + stmts + ["COMMIT;"]), fetch=False)
    if carried:
        log(f"carried {carried} tales one stop further; {len(ended)} roads ended")
    return carried + len(ended)


# ---------------------------------------------------------------------------
# storing

def store(start, end, faction, scribe, title, body, reports, model, era, flag, tales):
    stmts = ["START TRANSACTION;",
             f"SET @old = (SELECT id FROM chronicle_entry WHERE window_start = {q(stamp(start))} AND faction = {q(faction)});",
             "DELETE FROM chronicle_rumour WHERE entry_id = @old;",
             "DELETE FROM chronicle_tale WHERE entry_id = @old;",
             "DELETE FROM chronicle_entry WHERE id = @old;",
             "INSERT INTO chronicle_entry (window_start, window_end, faction, scribe, title, body, facts, model, era, flag) "
             f"VALUES ({q(stamp(start))}, {q(stamp(end))}, {q(faction)}, {q(scribe['name'])}, {q(title)}, {q(body)}, "
             f"{q(json.dumps([('(word of the ' + SCRIBES[OTHER[faction]]['faction'] + ') ' if r['enemy'] else '') + r['text'] for r in reports]))}, "
             f"{q(model)}, {q(era)}, {q(flag[:255]) if flag else 'NULL'});",
             "SET @entry = LAST_INSERT_ID();"]
    told, team, places = stamp(end), scribe["team"], 0
    for t in tales:
        origin = t["origin"]
        root = "NULL"
        if t["near"]:
            stmts += [tale_sql("@entry", "NULL", "NULL", team, "ours", 0, origin, t["near"], told, told),
                      "SET @near = LAST_INSERT_ID();",
                      places_sql("@entry", "@near", [origin], team, 0, t["near"], told, NEAR_HOURS)]
            root, places = "@near", places + 1
        # Our news is carried on to the next places; word of the enemy is first heard where it happened.
        onward = next_places([origin], [origin], origin, faction) if t["kind"] == "ours" else [origin]
        stmts += [tale_sql("@entry", root, root, team, t["kind"], 1, origin, t["far"], told, None),
                  "SET @far = LAST_INSERT_ID();",
                  places_sql("@entry", "@far", onward, team, 1, t["far"], told, FAR_HOURS)]
        places += len(onward)
    stmts += ["DELETE FROM chronicle_rumour WHERE expires_at < NOW() - INTERVAL 7 DAY;", "COMMIT;"]
    sql("\n".join(s for s in stmts if s), fetch=False)
    return places


def migrate():
    """Round two: rumour rows written before tales existed become tellings that no longer travel."""
    have = {r[0].lower() for r in sql("SELECT column_name FROM information_schema.columns WHERE table_schema = DATABASE() "
                                      "AND table_name = 'chronicle_rumour'")}
    if "tale_id" in have:
        return
    sql("ALTER TABLE chronicle_rumour ADD COLUMN tale_id BIGINT UNSIGNED NULL AFTER entry_id, ADD KEY by_tale (tale_id)",
        fetch=False)
    rows = sql("SELECT r.id, r.entry_id, r.zone_id, r.team, r.distance, HEX(r.words), "
               "DATE_FORMAT(c.window_end, '%Y-%m-%d %H:%i:%s') FROM chronicle_rumour r "
               "JOIN chronicle_entry c ON c.id = r.entry_id ORDER BY r.entry_id, r.id")
    # Each near row was stored followed by its far rows: pair them in id order.
    tellings, near = [], None
    for rid, entry, zone, team, distance, words, told in rows:
        words = bytes.fromhex(words).decode()
        last = tellings[-1] if tellings else None
        if distance == "0":
            near = dict(entry=entry, team=team, hop=0, origin=zone, words=words, told=told, rows=[rid], parent=None)
            tellings.append(near)
        elif last and last["hop"] == 1 and last["entry"] == entry and last["words"] == words:
            last["rows"].append(rid)
        else:
            parent = near if near and near["entry"] == entry else None
            tellings.append(dict(entry=entry, team=team, hop=1, origin=parent["origin"] if parent else zone, words=words,
                                 told=told, rows=[rid], parent=parent))
    stmts = ["START TRANSACTION;"]
    for i, t in enumerate(tellings):
        t["var"] = f"@t{i}"
        link = t["parent"]["var"] if t["parent"] else "NULL"
        stmts += [tale_sql(t["entry"], link, link, t["team"], "ours", t["hop"], t["origin"], t["words"], t["told"], t["told"]),
                  f"SET {t['var']} = LAST_INSERT_ID();",
                  f"UPDATE chronicle_rumour SET tale_id = {t['var']} WHERE id IN ({','.join(t['rows'])});"]
    sql("\n".join(stmts + ["COMMIT;"]), fetch=False)
    log(f"migrated {len(rows)} rumours into {len(tellings)} tellings")


# ---------------------------------------------------------------------------

def chronicle(start, faction, args, judge, until=None):
    end = start + dt.timedelta(hours=WATCH_HOURS)
    cut = min(end, until) if until else end
    scribe, e = SCRIBES[faction], eras.get(args.era)
    reports, known = watch_reports(start, cut, faction)
    title = title_of(start)
    if len(reports) < MIN_REPORTS:
        log(f"{title} ({scribe['faction']}): a quiet watch, {len(reports)} reports")
        if not args.dry_run:
            store(start, cut, faction, scribe, title, "", reports, "quiet", args.era, None, [])
        return
    t0 = time.time()
    title, body, model, flag = write_entry(scribe, e, start, reports, known, lanes_from(args.entry_lanes), judge)
    tales = write_rumours(scribe, e, start, reports, known, lanes_from(args.rumour_lanes), judge)
    if args.dry_run:
        print(f"\n== {title} — {scribe['name']} ({model}{', FLAG ' + flag if flag else ''})\n\n{body}\n")
        for t in tales:
            print(f"  [{t['kind']} · {place_name(t['origin'])}] near: {t['near'] or '—'}\n    far:  {t['far']}")
        return
    n = store(start, cut, faction, scribe, title, body, reports, model, args.era, flag, tales)
    heard = sum(t["kind"] == "enemy" for t in tales)
    log(f"{title} ({scribe['faction']}): {len(body.split())} words by {model}, {len(tales) - heard} tales and {heard} of "
        f"the enemy in {n} places{', judge flag kept: ' + flag if flag else ''} ({time.time() - t0:.0f}s)")


# ---------------------------------------------------------------------------
# the personal edition: only what one player's own characters (their main and alts) took part in

PERSONAL = dict(key="P", name="Sela Quillmere", faction="",
                about="a chronicler in Booty Bay who pays tavern-keepers, ship hands and caravan drivers for word of "
                      "a few travellers whose fortunes she follows, whichever banner they ride under")
PERSONAL_WORDS = [(2, 60, 120), (5, 100, 180), (MAX_REPORTS, 150, 250)]   # (most reports, fewest words, most words)
COMPANION_SECONDS = 90         # another's kill of the same foe this close to ours: they fought it together
PERSONAL_MOMENT = 4            # regard strength that makes words worth a line (the factions need 5)
BONDS = {"kin": "{pos} kin", "sworn": "sworn to {obj}", "debt": "bound to {obj} by a debt",
         "comrade": "{pos} comrade in arms", "acquaintance": "an acquaintance of {pos}"}
PRONOUNS = {0: ("he", "him", "his"), 1: ("she", "her", "her")}


def households():
    """{main guid: household} for every player who named a main (player_main): the main first, then their alts."""
    out = {}
    for main, guid, name, kind, race, cls, gender, bond in sql(
            "SELECT pk.main_guid, c.guid, c.name, pk.kind, c.race, c.class, c.gender, COALESCE(la.kind, '') "
            "FROM person_kind pk JOIN characters c ON c.guid = pk.guid LEFT JOIN lore_alt la ON la.guid = pk.guid "
            "WHERE pk.kind IN ('main', 'alt') AND pk.main_guid IS NOT NULL ORDER BY pk.main_guid, pk.kind DESC, c.name"):
        house = out.setdefault(int(main), dict(main=int(main), members=[]))
        house["members"].append(dict(guid=int(guid), name=name, kind=kind, race=int(race), cls=int(cls),
                                     gender=int(gender), bond=bond))
    for house in out.values():
        head = house["members"][0]
        house.update(name=head["name"], faction=faction_of_race(head["race"]))
    return {m: h for m, h in out.items() if h["members"][0]["kind"] == "main"}


def household_line(house):
    """"Durgan the Dwarf warrior (he), and those bound to him: Maevis the Dwarf hunter (she; his kin)"."""
    def one(m):
        return f"{m['name']} the {rg.RACES.get(m['race'], 'wanderer')} {rg.CLASSES.get(m['cls'], 'traveller')}"
    head, rest = house["members"][0], house["members"][1:]
    he, him, his = PRONOUNS.get(head["gender"], PRONOUNS[0])
    line = f"{one(head)} ({he})"
    if rest:
        bond = lambda m: BONDS.get(m["bond"], "bound to {obj}").format(obj=him, pos=his)
        line += f", and those bound to {him}: " + rg.join_names(
            [f"{one(m)} ({PRONOUNS.get(m['gender'], PRONOUNS[0])[0]}; {bond(m)})" for m in rest], cap=len(rest))
    return line


def where_words(zone):
    return rg.land_name(zone) if zone in rg.LAND else place_name(zone)


def json_detail(cols):
    try:
        d = json.loads("\t".join(cols))
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def gather_personal(start, end, house):
    """The watch's reports that the household's characters took part in: foes they felled and fell to, where they
    hunted and travelled, the tasks they saw through, how they grew, words with others, their companies' doings."""
    mine = [m["guid"] for m in house["members"]]
    ids = ",".join(map(str, mine))
    names = rg.company_names()
    window = f"e.ts >= {q(stamp(start))} AND e.ts < {q(stamp(end))}"
    reports = Reports()
    rows = [(int(r[0]), int(r[1]), int(r[2]), float(r[3]), r[4], json_detail(r[5:])) for r in sql(
        "SELECT e.actor_guid, e.zone_id, e.map_id, UNIX_TIMESTAMP(e.ts), e.event_type, COALESCE(e.detail, '{}') "
        f"FROM ledger_event e WHERE e.actor_guid IN ({ids}) AND {window} "
        "AND e.event_type IN ('kill', 'death', 'quest_complete', 'level_up', 'zone_change') ORDER BY e.ts, e.id")]
    moments = sql("SELECT l.bot_guid, l.other_guid, l.delta, l.reason FROM regard_log l "
                  f"WHERE l.source = 'chat' AND l.ts >= {q(stamp(start))} AND l.ts < {q(stamp(end))} "
                  f"AND ABS(l.delta) >= {PERSONAL_MOMENT} AND (l.bot_guid IN ({ids}) OR l.other_guid IN ({ids}))")
    land = lambda zone, map_id: rg.land_of(zone, map_id)
    by_kind = lambda kind: [r for r in rows if r[4] == kind]

    # Who else struck at the same fearsome foes, in the same land, at the same time.
    notable = [r for r in by_kind("kill") if "entry" in r[5] and (int(r[5].get("rank") or 0) > 0 or is_boss(r[5].get("boss")))]
    felled = sorted({int(r[5]["entry"]) for r in notable})
    entries = sorted(set(felled) | {int(r[5]["entry"]) for r in by_kind("death") if "entry" in r[5]})
    others = [(int(r[0]), int(r[1]), int(r[2]), float(r[3]), int(float(r[4]))) for r in sql(
        "SELECT e.actor_guid, e.zone_id, e.map_id, UNIX_TIMESTAMP(e.ts), JSON_EXTRACT(e.detail, '$.entry') "
        f"FROM ledger_event e WHERE e.event_type = 'kill' AND {window} AND e.actor_guid NOT IN ({ids}) "
        f"AND e.detail LIKE '{{%' AND JSON_EXTRACT(e.detail, '$.entry') IN ({','.join(map(str, felled))})")] \
        if felled else []
    ranks = {int(r[0]): (int(r[1]), int(r[2])) for r in sql(
        f"SELECT entry, `rank`, (SELECT COUNT(*) FROM acore_world.creature c WHERE c.id = ct.entry) "
        f"FROM acore_world.creature_template ct WHERE entry IN ({','.join(map(str, entries))})")} if entries else {}
    many = lambda entry: ranks.get(entry, (0, 1))[1] > 1
    parties = rg.Parties(end)
    # Only the delves this house actually went on.
    delves = [d for d in find_delves(window, parties) if set(d["guids"]) & set(mine)]

    friends = {m[0] for m in moments} | {m[1] for m in moments} | {o[0] for o in others} \
        | {g for d in delves for g in d["guids"]}
    people = rg.people(mine + [int(g) for g in friends])
    reports.names.update(p["name"] for p in people.values())
    reports.names.update(names[p["guild"]] for p in people.values() if p["guild"] in names)

    # Fearsome foes felled, with whoever fought beside them.
    deeds, strongholds = {}, {}
    for g, zone, map_id, ts, _, d in notable:
        where = land(zone, map_id)
        if not where:
            continue
        deed = deeds.setdefault((int(d["entry"]), where), dict(name=d.get("name", ""), rank=int(d.get("rank") or 0),
                                                                 boss=is_boss(d.get("boss")), guids=[], times=[],
                                                                 place=rg.deed_place(zone, map_id)))
        deed["times"].append(ts)
        if g not in deed["guids"]:
            deed["guids"].append(g)
    for (entry, where), deed in deeds.items():
        beside = [o[0] for o in others if o[4] == entry and land(o[1], o[2]) == where and o[0] in people
                  and any(abs(o[3] - t) <= COMPANION_SECONDS for t in deed["times"])]
        guids = deed["guids"] + [g for g in dict.fromkeys(beside) if g not in deed["guids"]]
        reports.names.add(deed["name"])
        place = deed["place"] or rg.land_name(where)
        reports.names.add(place)
        more = " more than once" if len(deed["times"]) > 1 else ""
        if deed["boss"] and place in INSTANCE_PLACES:
            continue        # told as one delve below
        if deed["boss"]:
            text = f"{cap(who(guids, people, names))} slew {foe(deed['name'], many(entry))}, the master of {place}{more}."
        else:
            text = (f"{cap(who(guids, people, names))} slew {foe(deed['name'], many(entry))}, "
                    f"{KILL_WORDS.get(deed['rank'], 'a fearsome foe')}, in {place}{more}.")
        reports.add(text, (8 if deed["boss"] else KILL_WEIGHT.get(deed["rank"], 3)) + len(guids) - 1, where)
    for d in delves:
        # The house's own lead the telling, whoever landed the blows.
        here = [g for g in d["guids"] if g in mine] + [g for g in d["guids"] if g not in mine]
        keep_stronghold(strongholds, d["place"], d["where"], d["names"], here)
    tell_strongholds(reports, strongholds, people, names)

    # The rest of their hunting, land by land.
    hunts = {}
    for g, zone, map_id, ts, _, d in by_kind("kill"):
        where = land(zone, map_id)
        if where:
            hunt = hunts.setdefault(where, dict(guids=[], foes={}))
            if g not in hunt["guids"]:
                hunt["guids"].append(g)
            name = d.get("name")
            if name and not DIGITS.search(name):
                hunt["foes"][name] = hunt["foes"].get(name, 0) + 1
    for where, hunt in sorted(hunts.items(), key=lambda kv: -sum(kv[1]["foes"].values()))[:BUSY_LANDS]:
        n = sum(hunt["foes"].values())
        common = [f"the {name}" for name, _ in sorted(hunt["foes"].items(), key=lambda kv: -kv[1])[:2]]
        reports.add(f"In {rg.land_name(where)}, {who(hunt['guids'], people, names)} brought down "
                    f"{count_words(n, 'foe', 'foes and beasts')}"
                    + (f"; those most often met were {rg.join_names(common, cap=2)}" if n > 1 and common else "") + ".",
                    2 + min(n, 40) / 20, where)

    # Struck down, by a foe or by the land itself.
    falls = {}
    for g, zone, map_id, ts, _, d in by_kind("death"):
        where = land(zone, map_id) or (zone if zone in CITIES else 0)
        key = (int(d["entry"]) if "entry" in d else 0, where)
        fall = falls.setdefault(key, dict(name=d.get("name", ""), guids=[], times=0,
                                          place=rg.deed_place(zone, map_id)))
        fall["times"] += 1
        if g not in fall["guids"]:
            fall["guids"].append(g)
    for (entry, where), fall in falls.items():
        one = len(fall["guids"]) == 1
        at = f" in {fall['place'] or where_words(where)}" if (where or fall["place"]) else ""
        again_ = " more than once" if fall["times"] > 1 else ""
        if entry and fall["name"]:
            reports.names.add(fall["name"])
            rank = ranks.get(entry, (0, 1))[0]
            reports.add(f"{cap(who(fall['guids'], people, names))} {'was' if one else 'were'} struck down by "
                        f"{foe(fall['name'], many(entry))}{at}{again_}, but {'lives' if one else 'live'} to fight again.",
                        3 + KILL_WEIGHT.get(rank, 0) / 2, where)
        else:
            reports.add(f"{cap(who(fall['guids'], people, names))} fell to the rocks or the water{at}{again_}, with no "
                        f"hand upon them, but {'lives' if one else 'live'} to fight again.", 2.5, where)

    # The tasks they saw through, land by land.
    done = {}
    for g, zone, map_id, ts, _, d in by_kind("quest_complete"):
        where, title = land(zone, map_id) or (zone if zone in CITIES else 0), str(d.get("title") or "").strip()
        if title and not DIGITS.search(title):
            task = done.setdefault(where, dict(guids=[], titles=[]))
            if g not in task["guids"]:
                task["guids"].append(g)
            if title not in task["titles"]:
                task["titles"].append(title)
    for where, task in sorted(done.items(), key=lambda kv: -len(kv[1]["titles"])):
        shown = [f'"{t}"' for t in task["titles"][:5]]
        more = ", and others besides" if len(task["titles"]) > 5 else ""
        tasks = f"the task known as {shown[0]}" if len(shown) == 1 else \
            f"the tasks known as {rg.join_names(shown, cap=len(shown))}{more}"
        reports.add(f"{cap(who(task['guids'], people, names))} saw through {tasks}"
                    f"{' in ' + where_words(where) if where else ''}.", 1.5 + min(len(task['titles']), 6) / 2, where)

    # Growing in strength.
    for g in mine:
        rises = [int(r[5]["new"]) for r in by_kind("level_up") if r[0] == g and "new" in r[5]]
        if not rises or g not in people:
            continue
        mark = max((n for n in rises if n in MILESTONE), default=0)
        reports.add(f"{describe(people[g], names)} grew stronger{' more than once' if len(rises) > 1 else ''}"
                    + (f", and {MILESTONE[mark]}" if mark else "") + ".", 2 + len(rises) / 2 + (mark / 20 if mark else 0))

    # The roads they took.
    paths = {}
    for g in mine:
        path = []
        for r in rows:
            if r[0] != g or r[4] != "zone_change":
                continue
            here = land(r[1], r[2]) or (r[1] if r[1] in CITIES else 0)
            came = int(r[5].get("from") or 0)
            if not path and came and (came in rg.LAND or came in CITIES) and came != here:
                path.append(came)
            if here and (not path or path[-1] != here):
                path.append(here)
        if len(path) >= 2:
            paths.setdefault(tuple(path), []).append(g)
    for path, guids in paths.items():
        via = list(dict.fromkeys(where_words(z) for z in path[1:-1] if z not in (path[0], path[-1])))
        if len(via) > 2:
            via = via[:2] + ["other places"]
        reports.add(f"{cap(who(guids, people, names))} journeyed from {where_words(path[0])}"
                    + (f" by way of {rg.join_names(via, cap=3)}" if via else "")
                    + f" to {where_words(path[-1])}.", 1.5 + min(len(path), 4) / 4, path[-1] if path[-1] in rg.LAND else 0)

    # Words that mattered, as the other remembers them.
    seen = set()
    for bot, other, delta, reason in sorted(moments, key=lambda m: -abs(float(m[2]))):
        a, b = people.get(int(bot)), people.get(int(other))
        if not a or not b or reason in seen or len(seen) >= 3:
            continue
        seen.add(reason)
        reports.add(f"Words passed between {a['name']} and {b['name']}; as {a['name']} remembers it: {reason}",
                    2 + abs(float(delta)) / 5)

    # Their companies.
    theirs = {people[g]["guild"] for g in mine if g in people and people[g]["guild"]}
    if theirs:
        for a, b, zone, kind, detail in sql(
                "SELECT i.guild_a, COALESCE(i.guild_b, 0), COALESCE(i.zone_id, 0), i.kind, i.detail FROM guild_incident i "
                f"WHERE i.ts >= {q(stamp(start))} AND i.ts < {q(stamp(end))} "
                f"AND (i.guild_a IN ({','.join(map(str, theirs))}) OR i.guild_b IN ({','.join(map(str, theirs))})) "
                "ORDER BY i.id LIMIT 6"):
            zone = int(zone)
            reports.add(detail.rstrip(".") + ".", INCIDENT_WEIGHT.get(kind, 4), zone if zone in rg.LAND else 0)
    return reports


def personal_words(n):
    return next((lo, hi) for most, lo, hi in PERSONAL_WORDS if n <= most)


def personal_prompt(house, e, start, reports):
    s = PERSONAL
    lo, hi = personal_words(len(reports))
    lines = "\n".join(f"- {r['text']}" for r in reports)
    return (f"You are {s['name']}, {s['about']}. The world you live in: {e['setting']}\n\n"
            f"Among those you follow are {household_line(house)}. You keep a book of their doings alone. "
            f"Write its entry for {WATCH_SPAN[start.hour]}. This is all the word of them that reached you:\n{lines}\n\n"
            f"Write it as {s['name']} would, in plain prose of about {lo} to {hi} words, "
            f"{'one paragraph' if hi <= 120 else 'one to three paragraphs'}. Keep to them: others appear only as the "
            "reports bring them in. Name the people, companies, foes and places given, and let your own voice and your "
            "fondness for them colour it. Use only these reports: invent no other deeds, deaths, people or places. Those "
            "struck down were wounded, not slain; everyone named lives. No figures or digits (say \"a handful\", "
            "\"scores\"), no title, headings, lists or markdown.")


def store_personal(start, end, house, title, body, reports, model, era, flag):
    sql("\n".join([
        "START TRANSACTION;",
        f"DELETE FROM chronicle_personal WHERE window_start = {q(stamp(start))} AND main_guid = {house['main']};",
        "INSERT INTO chronicle_personal (window_start, window_end, main_guid, scribe, title, body, facts, model, era, flag) "
        f"VALUES ({q(stamp(start))}, {q(stamp(end))}, {house['main']}, {q(PERSONAL['name'])}, {q(title)}, {q(body)}, "
        f"{q(json.dumps([r['text'] for r in reports]))}, {q(model)}, {q(era)}, {q(flag[:255]) if flag else 'NULL'});",
        "COMMIT;"]), fetch=False)


def chronicle_personal(start, house, args, judge, until=None):
    end = start + dt.timedelta(hours=WATCH_HOURS)
    cut = min(end, until) if until else end
    e = eras.get(args.era)
    gathered = gather_personal(start, cut, house)
    reports = gathered.best()
    title = title_of(start)
    if not reports:
        log(f"{title} ({house['name']}'s own): a quiet watch")
        if not args.dry_run:
            store_personal(start, cut, house, title, "", [], "quiet", args.era, None)
        return
    t0 = time.time()
    lo, _ = personal_words(len(reports))
    title, body, model, flag = write_prose(personal_prompt(house, e, start, reports), title, e, gathered.names,
                                           lanes_from(args.entry_lanes), judge, int(lo * 0.6))
    if args.dry_run:
        print(f"\n== {title} — {house['name']}'s own, by {PERSONAL['name']} ({model}{', FLAG ' + flag if flag else ''})"
              f"\n\n{body}\n")
        return
    store_personal(start, cut, house, title, body, reports, model, args.era, flag)
    log(f"{title} ({house['name']}'s own): {len(body.split())} words from {len(reports)} reports by {model}"
        f"{', judge flag kept: ' + flag if flag else ''} ({time.time() - t0:.0f}s)")


# ---------------------------------------------------------------------------
# dashboard

def snapshot():
    entries = sql("SELECT id, UNIX_TIMESTAMP(window_start), UNIX_TIMESTAMP(window_end), faction, scribe, title, model, "
                  "COALESCE(flag, ''), HEX(body), HEX(facts) FROM chronicle_entry WHERE body <> '' "
                  "ORDER BY window_start DESC, faction LIMIT 60")
    ids = [r[0] for r in entries]
    tales = {}
    if ids:
        for tid, eid, root, kind, hop, origin, words, told, zones in sql(
                "SELECT t.id, t.entry_id, COALESCE(t.root_id, t.id), t.kind, t.hop, t.origin, HEX(t.words), "
                "UNIX_TIMESTAMP(t.told_at), COALESCE(GROUP_CONCAT(r.zone_id ORDER BY r.id), '') FROM chronicle_tale t "
                f"LEFT JOIN chronicle_rumour r ON r.tale_id = t.id WHERE t.entry_id IN ({','.join(ids)}) "
                "GROUP BY t.id ORDER BY t.id"):
            tales.setdefault(eid, []).append(dict(
                id=int(tid), root=int(root), kind=kind, hop=int(hop), origin=place_name(int(origin)),
                words=bytes.fromhex(words).decode(), told=int(float(told)),
                places=[place_name(int(z)) for z in zones.split(",") if z not in ("", "NULL")]))
    houses = households()
    personal = sql("SELECT id, UNIX_TIMESTAMP(window_start), UNIX_TIMESTAMP(window_end), main_guid, scribe, title, model, "
                   "COALESCE(flag, ''), HEX(body), HEX(facts) FROM chronicle_personal WHERE body <> '' "
                   f"ORDER BY window_start DESC, main_guid LIMIT {60 * max(1, len(houses))}")
    out = dict(generated=int(time.time()),
               scribes={k: dict(name=s["name"], faction=s["faction"], about=s["about"]) for k, s in SCRIBES.items()},
               personal_scribe=dict(name=PERSONAL["name"], about=PERSONAL["about"]),
               households={str(m): dict(main=m, name=h["name"], faction=h["faction"],
                                        members=[dict(guid=x["guid"], name=x["name"], kind=x["kind"]) for x in h["members"]])
                           for m, h in houses.items()},
               personal=[dict(id=int(r[0]), start=int(float(r[1])), end=int(float(r[2])), main=int(r[3]), scribe=r[4],
                              title=r[5], model=r[6], flag=r[7], body=bytes.fromhex(r[8]).decode(),
                              facts=json.loads(bytes.fromhex(r[9]).decode()))
                         for r in personal],
               entries=[dict(id=int(r[0]), start=int(float(r[1])), end=int(float(r[2])), faction=r[3], scribe=r[4],
                             title=r[5], model=r[6], flag=r[7], body=bytes.fromhex(r[8]).decode(),
                             facts=json.loads(bytes.fromhex(r[9]).decode()), tales=tales.get(r[0], []))
                        for r in entries])
    tmp = SNAPSHOT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, SNAPSHOT)
    return len(entries) + len(personal)


# ---------------------------------------------------------------------------

def written():
    return {(r[0], r[1]): r[2] for r in sql(
        "SELECT DATE_FORMAT(window_start, '%Y-%m-%d %H:%i:%s'), faction, DATE_FORMAT(window_end, '%Y-%m-%d %H:%i:%s') "
        "FROM chronicle_entry")}


def written_personal():
    return {(r[0], int(r[1])): r[2] for r in sql(
        "SELECT DATE_FORMAT(window_start, '%Y-%m-%d %H:%i:%s'), main_guid, DATE_FORMAT(window_end, '%Y-%m-%d %H:%i:%s') "
        "FROM chronicle_personal")}


def catch_up(args, judge):
    now = dt.datetime.now()
    newest = watch_start(now) - dt.timedelta(hours=WATCH_HOURS)
    first = max(watch_start(now - dt.timedelta(days=CATCHUP_DAYS)), dt.datetime.combine(FIRST_DAY, dt.time()))
    return write_watches(first, newest, args, judge)


def write_watches(first, last, args, judge, force=False):
    """Every watch from first to last that is not written yet (or everything, forced): the factions', then each
    player's own."""
    done, mine, houses = written(), written_personal(), households()
    start = first
    wrote = 0
    while start <= last:
        end = start + dt.timedelta(hours=WATCH_HOURS)
        jobs = [] if args.personal == "only" else \
            [(f"{title_of(start)} ({f})", done.get((stamp(start), f)), lambda f=f: chronicle(start, f, args, judge))
             for f in ([args.faction] if getattr(args, "faction", None) else SCRIBES)]
        jobs += [] if not args.personal else \
            [(f"{title_of(start)} ({h['name']}'s own)", mine.get((stamp(start), m)),
              lambda h=h: chronicle_personal(start, h, args, judge)) for m, h in houses.items()]
        for label, stored_end, job in jobs:
            # An entry written early (write --open) is written again once its watch has closed.
            if force or stored_end is None or stored_end < stamp(end):
                try:
                    job()
                    wrote += 1
                except Exception as ex:  # a backend or the database went away: the watch waits for the next cycle
                    log(f"{label} waits: {type(ex).__name__}: {ex}")
        start = end
    return wrote


def parse_watch(s):
    if not s:
        return watch_start(dt.datetime.now())
    return watch_start(dt.datetime.strptime(s, "%Y-%m-%d %H"))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "write", "facts", "spread"):
        p = sub.add_parser(name)
        p.add_argument("--era", default="classic")
        p.add_argument("--entry-lanes", default=ENTRY_LANES)
        p.add_argument("--rumour-lanes", default=RUMOUR_LANES)
        p.add_argument("--dry-run", action="store_true")
        if name == "run":
            p.add_argument("--interval", type=int, default=600)
        elif name != "spread":
            p.add_argument("--watch", help='"YYYY-MM-DD HH" (any hour inside the watch); default: the current watch')
            p.add_argument("--faction", choices=sorted(SCRIBES))
            p.add_argument("--personal", action="store_const", const="only", default=False,
                           help="each player's own characters, instead of the factions")
        if name == "write":
            p.add_argument("--force", action="store_true", help="rewrite a watch already written")
            p.add_argument("--open", action="store_true", help="write a watch that has not closed yet, up to now")
            p.add_argument("--since", help='"YYYY-MM-DD HH": every closed watch from then on')
    sub.add_parser("snapshot")
    args = ap.parse_args()
    if args.cmd == "run":
        args.personal = True

    if args.cmd != "facts":
        sql(open(os.path.join(HERE, "chronicle_tables.sql")).read(), fetch=False)
        migrate()
    if args.cmd == "snapshot":
        print(f"{snapshot()} entries")
        return 0

    if args.cmd == "facts":
        start = parse_watch(args.watch)
        if args.personal:
            for house in households().values():
                print(f"== {title_of(start)} — {house['name']}'s own: {household_line(house)}")
                for r in gather_personal(start, start + dt.timedelta(hours=WATCH_HOURS), house).best():
                    print(f"  {r['weight']:5.1f}  {r['text']}")
            return 0
        for faction in [args.faction] if args.faction else SCRIBES:
            print(f"== {title_of(start)} — {SCRIBES[faction]['faction']}")
            reports, _ = watch_reports(start, start + dt.timedelta(hours=WATCH_HOURS), faction)
            for r in reports:
                print(f"  {r['weight']:5.1f}  {'(enemy) ' if r['enemy'] else ''}{r['text']}")
        return 0

    judge = fleet.Judge(slots=1)
    if args.cmd == "spread":
        n = spread(args, judge)
        if n and not args.dry_run:
            snapshot()
        return 0

    if args.cmd == "write" and args.since:
        last = watch_start(dt.datetime.now()) - dt.timedelta(hours=WATCH_HOURS)
        write_watches(parse_watch(args.since), last, args, judge, force=args.force)
        if not args.dry_run:
            snapshot()
        return 0

    if args.cmd == "write":
        start = parse_watch(args.watch)
        end = start + dt.timedelta(hours=WATCH_HOURS)
        now = dt.datetime.now()
        if end > now and not args.open:
            sys.exit(f"{title_of(start)} has not closed yet (until {stamp(end)}); pass --open to write it up to now")
        if args.personal:
            done = written_personal()
            for main, house in households().items():
                if (stamp(start), main) in done and not (args.force or args.dry_run):
                    log(f"{title_of(start)} ({house['name']}'s own) is already written; --force to rewrite")
                    continue
                chronicle_personal(start, house, args, judge, until=now if args.open else None)
            if not args.dry_run:
                snapshot()
            return 0
        done = written()
        for faction in [args.faction] if args.faction else SCRIBES:
            if (stamp(start), faction) in done and not (args.force or args.dry_run):
                log(f"{title_of(start)} ({faction}) is already written; --force to rewrite")
                continue
            chronicle(start, faction, args, judge, until=now if args.open else None)
        if not args.dry_run:
            snapshot()
        return 0

    while True:
        t0 = time.time()
        if os.path.exists(PAUSE_FILE):
            log("paused (PAUSE file present)")
        else:
            try:
                changed = catch_up(args, judge)
                changed += spread(args, judge)
                if changed:
                    snapshot()
            except Exception as ex:
                log(f"cycle failed: {type(ex).__name__}: {ex}")
        time.sleep(max(30, args.interval - (time.time() - t0)))


if __name__ == "__main__":
    sys.exit(main())
