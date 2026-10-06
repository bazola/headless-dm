"""The Dungeon Master: decides what the world says, and to whom, ahead of the moment it is said.

custom wow plan 62 (plan 29 is the whole design). Phase 1, the scene channel: this service writes dm_line;
mod-ollama-chat (mod-ollama-chat_director.cpp) loads it and speaks it when the moment comes, from positions
only the worldserver has. The service never decides WHEN, and the worldserver never waits on a model.

The first scene is a dungeon's final boss speaking to a party as they come into sight of it (C9-1):
  - `boss-words` writes a few lines per final boss for anyone (instance 0). Run once; they are the
    fallback, and they let the C++ half be tested with no service running.
  - `run` follows ledger_event. When a real player walks into a dungeon (mod-ledger `instance_enter`, with
    the instance and the party), it writes the final boss's words for THAT party: who they are, whether
    this boss has met any of them before, what the realm says of them. The walk to the last boss is 20-60
    minutes; the line is ready in under one. mod-ledger's `dm_scene` says when a line was spoken.

The second scene is the innkeeper (C7-2a, phase 1d): when a real player comes into a zone that has an inn
(`zone_change`), `run` writes two things an innkeeper there might say to them: the talk going round the zone
(chronicle_rumour) and what the land says of them (chronicle_tale). The module speaks one when they walk up
to any innkeeper in that zone. Lines are keyed by player and zone, not by innkeeper.

Standing rule: bots are people living in Azeroth. Lines are in-world words: no figures, no game terms.

Usage:
  python3 dm.py boss-words [--map 36] [--force] [--dry-run]   party-free lines for the final bosses
  python3 dm.py run [--interval 10]                            service loop
  python3 dm.py cue <ledger_event id> [--dry-run]              write the party line for one instance_enter
  python3 dm.py try --map 36 --party 702,1611        a party line printed, never stored (no dungeon needed)
  python3 dm.py bosses                                          list the final bosses this era knows
  python3 dm.py inn --guid 702 [--zone 12] [--dry-run]          write a player's inn lines for a zone now
  python3 dm.py inns                                            list the innkeepers this era has, by zone
  python3 dm.py publish                                         rewrite dm.json for the dashboard now
Kill switch: `systemctl --user stop wow-dm`, create services/dm/PAUSE, or OllamaChat.Director.Enable = 0.
Site keys: DM_BOSS_CHANCE (percent of dungeon entries that get a party line, default 100),
           DM_INN_CHANCE (percent of a player's arrivals in an inn's zone that get inn lines, default 100).
"""
import argparse
import json
import os
import random
import re
import subprocess
import sys
import time

_SERVICES = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(_SERVICES, "lore"))
sys.path.insert(0, os.path.join(_SERVICES, "regard"))
import era as eras  # noqa: E402
import fleet  # noqa: E402
import regard as rg  # noqa: E402  (database helpers, people, lands)

import areas  # noqa: E402  (where a point is, from the terrain files)

sys.path.insert(0, _SERVICES)
from common import site  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PAUSE_FILE = os.path.join(HERE, "PAUSE")
# The dashboard's DM panel (plan 62 1e) reads this. Rewritten every PUBLISH_EVERY cycles and after anything new.
SNAPSHOT = os.path.join(site.get("DATA_DIR", "/opt/wow/server/data"), "dashboard-data", "dm.json")
PUBLISH_EVERY = 6
SNAPSHOT_DAYS = 7

LANES = "evo-quality,z13-qwen35"     # JSON replies: the novelist ignores formats
MAX_ATTEMPTS = 4
MAX_WORDS = 34
PARTY_LINE_HOURS = 6                 # a party line outlives any sensible run, and no more
SCENE = "boss_approach"
INN = "inn_gossip"
INN_LINE_HOURS = 6                   # long enough to reach the inn; short enough that the talk is fresh
INN_LINES = 2

RAIDS = {409, 249, 469, 309, 509, 531, 533}
# A final boss with no spawn row is summoned during its dungeon's event, so its map cannot be read from
# `creature`. Mutanus wakes at the end of Naralex's awakening in the Wailing Caverns.
SUMMONED_MAP = {3654: 43}
# Bosses whose stock script already greets a party on approach (SmartAI line-of-sight / linked intros). A
# second greeting on top of theirs is noise, so the DM writes nothing for them and the world stays silent.
OWN_INTRO = {4275: "Archmage Arugal", 3977: "High Inquisitor Whitemane"}

# Who each final boss is, in a line, so the model writes the person and not a generic lord of a dark place
# (the first pass had Bazil Thredd talking about the Lich King). Game lore, like regard's LAND_NOTES.
BOSS_NOTES = {
    1716: "a Defias Brotherhood captain who leads the prison riot in the Stockade, sworn to Edwin VanCleef",
    639: "the master stonemason who rebuilt Stormwind and was never paid; leader of the Defias Brotherhood, "
         "building a juggernaut of a ship in the mine beneath Moonbrook to take his revenge on the nobles",
    3654: "a monstrous thing born of the Emerald Nightmare that feeds on the druid Naralex's dreams",
    4421: "a quilboar matriarch of the Razormane who serves the death's head priests and their dark new allies",
    4829: "an ancient hydra in the drowned temple of Blackfathom, worshipped by the Twilight's Hammer",
    2748: "the stone-keeper titan watcher who guards the Discs of Norgannon in the hall of the Makers",
    7800: "the gnome who betrayed Gnomeregan to the troggs and flooded it with radiation, and now calls it his",
    5709: "the shade of a green dragon chained to the sunken temple of Atal'Hakkar by the Hakkari priests",
    7358: "a lich of the Scourge who has made the quilboar's holy downs into a tomb for his master",
    4543: "a Scarlet Crusade mage who died and was raised by his own blood magic in the graveyard he guards",
    6487: "the Scarlet Crusade's arcanist, keeper of the monastery library and its forbidden knowledge",
    3975: "the Scarlet Champion, the Crusade's greatest blademaster, who guards the armory",
    7267: "the Sandfury troll chief who rules Zul'Farrak with his pet, Ruuzlu",
    9568: "the Blackrock orc overlord of the lower spire, who answers to the black dragonflight",
    10363: "the black dragonflight's general who commands the upper Blackrock Spire for Nefarian",
    9018: "the Dark Iron interrogator who runs the prison of Blackrock Depths",
    9019: "the Dark Iron emperor of the Shadowforge, in thrall to Ragnaros, holding Princess Moira as his bride",
    1853: "the Cult of the Damned's headmaster of Scholomance, teaching necromancy in the halls of Caer Darrow",
    10440: "the Cult of the Damned's master of Stratholme, a lord of the Scourge in a burning city",
    12258: "a great beast that hunts the crystal caves of Maraudon",
    12236: "the last son of Zaetar, a corrupted centaur prince who guards the orange crystal path of Maraudon",
    12201: "the daughter of the earth elemental lord Therazane, who poisons the earth of Maraudon",
    11520: "a demon of the Burning Blade who feeds on the fires beneath Orgrimmar",
    11492: "a satyr of Dire Maul who corrupts the warpwood around the old highborne city",
    11486: "the last prince of the Shen'dralar highborne, who keeps Eldre'Thalas and its imprisoned demon",
    11501: "the ogre king who rules the Gordok in Dire Maul until someone stronger kills him",
}
# The first pass said the same few things in twenty-six mouths. Named here so the prompt can forbid them.
WORN = ("the stones/earth/walls remember, turn back now, rats, whispers, bones, breathes, echo, "
        "screams, last note/voice/echo")
MOODS = ["contempt", "mockery", "a bargain or an offer", "pride in what you have built or taken",
         "weariness", "zeal for your cause", "grief or bitterness", "amusement", "a promise of what you will do",
         "a question you want answered"]

# The maps an era's innkeepers stand on. Outland and Northrend inns have no one to talk to before their era.
ERA_MAPS = {"classic": {0, 1}, "tbc": {0, 1, 530}, "wotlk": {0, 1, 530, 571}}
# The terrain files place Ironforge and the Undercity in Dun Morogh and Tirisfal: both cities are underground,
# and the core takes their zone from the building (WMO), which the grid does not carry.
INN_ZONE_FIX = {5111: 1537, 6741: 1497}
CAPITALS = {1519: "Stormwind", 1537: "Ironforge", 1657: "Darnassus", 1637: "Orgrimmar", 1638: "Thunder Bluff",
            1497: "the Undercity"}

DIGITS = re.compile(r"\d")
GAME_WORDS = re.compile(r"\b(boss|dungeon|instance|raid|loot|aggro|pull|respawn|npc|mob|quest|player|party|"
                        r"group|level|tank|healer|dps)\b", re.I)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


sql, q = rg.sql, rg.q
_WORLD = site.db("world")


def ensure_schema():
    """dm_line's per-player columns (phase 1d). dm_tables.sql has them for a new install; an older table gets
    them here, because MySQL 8 has no ADD COLUMN IF NOT EXISTS."""
    have = {r[0] for r in sql("SELECT column_name FROM information_schema.columns WHERE table_schema = DATABASE() "
                              "AND table_name = 'dm_line'")}
    if have and "for_guid" not in have:
        sql("ALTER TABLE dm_line ADD COLUMN for_guid INT UNSIGNED NOT NULL DEFAULT 0 AFTER audience, "
            "ADD COLUMN zone_id INT UNSIGNED NOT NULL DEFAULT 0 AFTER for_guid, "
            "ADD KEY by_player (scene, for_guid, zone_id);", fetch=False)
        log("dm_line: added for_guid, zone_id")


def world_sql(query):
    host, port, user, pw, db = _WORLD
    out = subprocess.run(["mysql", "--default-character-set=utf8mb4", "-h", host, "-P", port, "-u", user, db,
                          "--batch", "--raw", "-N"], input=query, env={"MYSQL_PWD": pw, "PATH": "/usr/bin:/bin"},
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return [row.split("\t") for row in out.stdout.splitlines() if row]


def era():
    return eras.get(site.get("ERA", "classic"))


# ---------------------------------------------------------------------------
# the bosses

def final_bosses():
    """{entry: {name, map, place, voice}} for every final encounter of a dungeon this era has (raids left out).
    The voice is the boss's own stock lines: how it talks, never words to repeat."""
    rows = world_sql(
        "SELECT ie.creditEntry, t.name, (SELECT MIN(c.map) FROM creature c WHERE c.id = ie.creditEntry) "
        "FROM instance_encounters ie JOIN creature_template t ON t.entry = ie.creditEntry "
        "WHERE ie.creditType = 0 AND ie.lastEncounterDungeon <> 0")
    bosses = {}
    for entry, name, map_id in rows:
        entry = int(entry)
        map_id = int(map_id) if map_id not in ("NULL", "") else SUMMONED_MAP.get(entry)
        if map_id is None or map_id in RAIDS or map_id not in rg.lands.INSTANCE_NAMES or entry in OWN_INTRO:
            continue
        bosses[entry] = dict(entry=entry, name=name, map=map_id, place=rg.lands.INSTANCE_NAMES[map_id], voice=[])
    if bosses:
        for entry, text in world_sql("SELECT CreatureID, Text FROM creature_text WHERE Type IN (12, 14) "
                                     f"AND CreatureID IN ({','.join(map(str, bosses))}) ORDER BY CreatureID, GroupID"):
            bosses[int(entry)]["voice"].append(text)
    return bosses


# ---------------------------------------------------------------------------
# writing and checking a line

def lanes():
    return fleet.prefer_lanes(LANES)


def attempt_on(ls, attempt):
    usable = [lane for lane in ls if lane.available()] or ls
    return usable[(attempt - 1) % len(usable)]


def clean(line):
    line = str(line).strip().strip('"').strip()
    line = re.sub(r"\s+", " ", line)
    return line.translate(rg.ASCII_PUNCT)


def check(line, e, boss, judge, known):
    """The reason a line may not be spoken, or None."""
    if not line:
        return "empty"
    if len(line.split()) > MAX_WORDS:
        return "too long"
    if len(line.encode()) > 255:
        return "too long for the table"
    if DIGITS.search(line):
        return "figures"
    m = e["meta"].search(line) or GAME_WORDS.search(line)
    if m:
        return f"out-of-world word {m.group(0)!r}"
    # Whole or in part: "Eh? What have we here?" said again in front of new words is still the stock line.
    if any(len(v) > 12 and clean(v).lower().rstrip(".!?") in line.lower() for v in boss["voice"]):
        return "a stock line repeated"
    if judge:
        flag = judge.check(eras.judge_prompt(e, line, known))
        if flag:
            return f"judge: {flag}"
    return None


def write_lines(prompt, boss, e, judge, known, want, purpose, stage=SCENE):
    """Up to `want` lines that pass every check, best first, and the model that wrote them."""
    ls = lanes()
    errors = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        lane = attempt_on(ls, attempt)
        try:
            out = lane.chat([{"role": "user", "content": prompt}], 600, temperature=0.9, json_mode=True,
                            context={"purpose": purpose, "source": "dm", "stage": stage, "route": "quality"})
            m = re.search(r"\{.*\}", out, re.S)
            items = json.loads(m.group(0)).get("lines", []) if m else []
            good = []
            for item in items:
                line = clean(item)
                why = check(line, e, boss, judge, known)
                if why:
                    log(f"  rejected ({why}): {line}")
                    continue
                good.append(line)
            if good:
                return good[:want], lane.model
            errors.append(f"{lane.name}: nothing usable")
        except Exception as ex:
            errors.append(f"{lane.name}: {ex}")
            log(f"  attempt {attempt} on {lane.name} failed: {ex}")
    raise RuntimeError("; ".join(errors))


def voice_block(boss):
    if not boss["voice"]:
        return ""
    sample = "\n".join(f"- {v}" for v in boss["voice"][:6])
    return ("How you speak when you fight (your manner only: never repeat these words):\n" + sample + "\n\n")


def who_you_are(boss):
    note = BOSS_NOTES.get(boss["entry"])
    return f"Who you are: {note}.\n\n" if note else ""


RULES = ("Rules: each line is one or two sentences and at most thirty words, spoken aloud as they come into sight, "
         "BEFORE any fight. You are a person of this world; say nothing a person in it could not say. "
         "No numbers or figures. No words from outside the world. Do not describe yourself in the third person, "
         "do not narrate actions, no stage directions. Speak as yourself about your own cause and enemies; avoid these "
         "worn images: " + WORN + ". Era: {setting}\n\n"
         'Reply with JSON only: {{"lines": ["...", "...", "..."]}}')


def anyone_prompt(boss, e):
    moods = random.sample(MOODS, 3)
    return (f"You are {boss['name']}, master of {boss['place']}. Intruders have fought their way to you and are "
            "coming into sight now. You do not know who they are.\n\n"
            + who_you_are(boss)
            + voice_block(boss)
            + f"Write three different things you might call out to them, one each in these moods: {', '.join(moods)}.\n\n"
            + RULES.format(setting=e["setting"]))


# ---------------------------------------------------------------------------
# boss-words: the lines for anyone

def boss_words(args):
    e = era()
    bosses = final_bosses()
    if args.map:
        bosses = {k: b for k, b in bosses.items() if b["map"] == args.map}
    have = {int(r[0]) for r in sql(f"SELECT DISTINCT speaker_entry FROM dm_line WHERE scene = {q(SCENE)} "
                                   "AND instance_id = 0")}
    judge = fleet.Judge()
    for b in sorted(bosses.values(), key=lambda b: b["map"]):
        if b["entry"] in have and not args.force:
            continue
        log(f"{b['name']} ({b['place']})")
        try:
            lines, model = write_lines(anyone_prompt(b, e), b, e, judge, [b["name"]], 3, "dm_boss_words")
        except RuntimeError as ex:
            log(f"  gave up: {ex}")
            continue
        for line in lines:
            log(f"  {line}")
        if args.dry_run:
            continue
        rows = ",".join(f"({q(SCENE)}, 0, {b['map']}, {b['entry']}, JSON_ARRAY(), {q(line)}, "
                        f"{q('for anyone')}, {i}, {q(model)})" for i, line in enumerate(lines))
        sql(f"DELETE FROM dm_line WHERE scene = {q(SCENE)} AND instance_id = 0 AND speaker_entry = {b['entry']}; "
            "INSERT INTO dm_line (scene, instance_id, map_id, speaker_entry, audience, words, why, `rank`, model) "
            f"VALUES {rows};", fetch=False)


# ---------------------------------------------------------------------------
# the party line

def levels(guids):
    if not guids:
        return {}
    return {int(g): int(lv) for g, lv in sql(f"SELECT guid, level FROM characters WHERE guid IN ({','.join(map(str, guids))})")}


def goals(guids):
    """What each companion lives for, in their own short words (bots and alts only; a main is the player's)."""
    if not guids:
        return {}
    return {int(g): m for g, m in sql("SELECT guid, motivation_short FROM lore_character "
                                      f"WHERE guid IN ({','.join(map(str, guids))}) AND motivation_short IS NOT NULL")}


def past_meetings(entry, guids):
    """{guid: (times, last date)} for every one of them who has stood over this boss's body before."""
    if not guids:
        return {}
    rows = sql("SELECT actor_guid, COUNT(*), DATE(MAX(ts)) FROM ledger_event WHERE event_type = 'kill' "
               f"AND JSON_EXTRACT(detail, '$.entry') = {int(entry)} AND actor_guid IN ({','.join(map(str, guids))}) "
               "GROUP BY actor_guid")
    return {int(g): (int(n), d) for g, n, d in rows}


def when(day):
    """A date as a person would say it. The prompt must not carry figures: the line is checked for them."""
    import datetime as dt
    d = (dt.date.today() - dt.date.fromisoformat(str(day)[:10])).days
    return ("earlier today" if d <= 0 else "yesterday" if d == 1 else "a few days ago" if d < 7
            else "some weeks ago" if d < 30 else "long ago")


RUN_HOURS = 3   # how long after its last entry a run is still that run (corpse runs re-enter the same instance)


def past_runs(boss, map_id, guids, current_instance, keep=3):
    """What became of the earlier runs at this boss by any of these people, newest first, from the ledger alone.

    Each run is one instance (corpse runs re-enter it). Its outcome is one of:
      killed     one of them killed the boss
      fell       the boss killed one of them, and was not killed
      fled       the boss spoke to them (dm_scene) and no fight followed
      never      they never reached it: no words, no fight. `deaths` says how hard the way in went
    Only runs since mod-ledger began recording instance_enter (plan 62) can be read; before that a kill is
    still known through past_meetings, but a failure is not."""
    want = set(guids)
    runs = {}
    for event_id, ts, actor, detail in sql(
            "SELECT id, ts, actor_guid, detail FROM ledger_event WHERE event_type = 'instance_enter' "
            f"AND map_id = {int(map_id)} ORDER BY id"):
        d = json.loads(detail or "{}")
        inst = int(d.get("instance", 0))
        members = {int(actor)} | {int(g) for g in d.get("party", [])}
        if not inst or inst == current_instance or not (members & want):
            continue
        r = runs.setdefault(inst, dict(instance=inst, start=ts, last=ts, members=set()))
        r["last"] = ts
        r["members"] |= members
    out = []
    for r in sorted(runs.values(), key=lambda r: r["start"], reverse=True)[:keep]:
        ids = ",".join(map(str, sorted(r["members"])))
        window = (f"ts >= {q(r['start'])} AND ts <= {q(r['last'])} + INTERVAL {RUN_HOURS} HOUR "
                  f"AND map_id = {int(map_id)} AND actor_guid IN ({ids})")
        kills = int(sql(f"SELECT COUNT(*) FROM ledger_event WHERE event_type = 'kill' AND {window} "
                        f"AND JSON_EXTRACT(detail, '$.entry') = {boss['entry']}")[0][0])
        deaths = sql(f"SELECT actor_guid, JSON_EXTRACT(detail, '$.entry') FROM ledger_event "
                     f"WHERE event_type = 'death' AND {window}")
        by_boss = {int(g) for g, entry in deaths if entry not in (None, "NULL") and int(entry) == boss["entry"]}
        spoken = sql("SELECT JSON_EXTRACT(detail, '$.line') FROM ledger_event WHERE event_type = 'dm_scene' "
                     f"AND JSON_EXTRACT(detail, '$.instance') = {r['instance']} "
                     f"AND JSON_EXTRACT(detail, '$.entry') = {boss['entry']} LIMIT 1")
        said = ""
        if spoken:
            row = sql(f"SELECT words FROM dm_line WHERE id = {int(spoken[0][0])}")
            said = row[0][0] if row else ""
        outcome = ("killed" if kills else "fell" if by_boss else "fled" if spoken else "never")
        out.append(dict(outcome=outcome, when=when(r["start"]), members=r["members"] & want,
                        deaths=len(deaths), fell=by_boss & want, said=said))
    return out


def tales_of(names, limit=3):
    """What the realm says of them: tales that name one of them, newest first."""
    if not names:
        return []
    like = " OR ".join(f"words LIKE {q('%' + n + '%')}" for n in names)
    return [r[0] for r in sql(f"SELECT words FROM chronicle_tale WHERE {like} ORDER BY told_at DESC LIMIT {limit}")]


def run_lines(runs, names):
    def them(guids):
        ns = [names[g] for g in guids if g in names]
        return " and ".join(ns) if len(ns) < 3 else ", ".join(ns[:-1]) + " and " + ns[-1]
    lines = []
    for r in runs:
        who, way = them(r["members"]), (f", and lost people on the way" if r["deaths"] else "")
        if r["outcome"] == "killed":
            lines.append(f"- {r['when']}: {who} came, and they put you down.")
        elif r["outcome"] == "fell":
            lines.append(f"- {r['when']}: {who} came for you, and you killed {them(r['fell']) or 'one of them'}. "
                         "They did not finish you.")
        elif r["outcome"] == "fled":
            lines.append(f"- {r['when']}: {who} came within sight of you, heard you, and left without a fight"
                         + (f'. You had told them: "{r["said"].rstrip(".!?")}."' if r["said"] else "."))
        else:
            lines.append(f"- {r['when']}: {who} came into your halls and never reached you{way}. "
                         "Your servants turned them back, or their nerve failed.")
    return lines


def party_prompt(boss, e, party, met, tales, wants, runs=()):
    who = []
    for p in party:
        race, calling = rg.RACES.get(p["race"], "wanderer"), rg.CLASSES.get(p["cls"], "traveller")
        who.append(f"- {p['name']}, a {race} {calling}")
    seen = []
    for p in party:
        if p["guid"] in met:
            n, day = met[p["guid"]]
            seen.append(f"- {p['name']} has stood over your body before"
                        + (" more than once" if n > 1 else "") + f" (last {when(day)}). You remember them, and it.")
    history = run_lines(runs, {p["guid"]: p["name"] for p in party})
    known = "\n".join(f"- {t}" for t in tales)
    hopes = "\n".join(f"- {p['name']}: {wants[p['guid']]}" for p in party if p["guid"] in wants)
    return (f"You are {boss['name']}, master of {boss['place']}. These people have fought their way to you and are "
            "coming into sight now:\n" + "\n".join(who) + "\n\n"
            + who_you_are(boss)
            + voice_block(boss)
            + ("You have met some of them before. In this world the fallen rise again, and so do you; you remember "
               "who put you down:\n" + "\n".join(seen) + "\n\n" if seen else "")
            + ("How their earlier attempts on you went:\n" + "\n".join(history) + "\n"
               "A failure is yours to mock: those who turned back, fled, or fell to you. Do not repeat what you "
               "told them before; answer it.\n\n" if history else "")
            + ("What is said of them in the land (rumour reaches even you):\n" + known + "\n\n" if known else "")
            + ("What drives some of them, which you may guess at or mock:\n" + hopes + "\n\n" if hopes else "")
            + "Write three different things you might call out to them. Speak to THEM: name one or two of them if you "
              "have reason to know them (an old meeting, a rumour); otherwise address them by what you can see, a "
              "dwarf, a priest, a band of thieves. If you remember them, or their failure, the first line must show it.\n\n"
            + RULES.format(setting=e["setting"]))


def answer_cue(cue_id, dry_run=False, judge=None):
    """Write the final boss's words for the party in one instance_enter. True when lines were written."""
    rows = sql(f"SELECT actor_guid, map_id, detail FROM ledger_event WHERE id = {int(cue_id)} "
               "AND event_type = 'instance_enter'")
    if not rows:
        log(f"cue {cue_id}: no such instance_enter")
        return False
    actor, map_id, detail = int(rows[0][0]), int(rows[0][1]), json.loads(rows[0][2] or "{}")
    instance = int(detail.get("instance", 0))
    if not instance or detail.get("raid"):
        return False
    guids = [actor] + [int(g) for g in detail.get("party", []) if int(g) != actor]
    return party_lines(map_id, instance, guids, cue_id, dry_run, judge, f"cue {cue_id}")


def party_lines(map_id, instance, guids, cue_id, dry_run, judge, label):
    bosses = [b for b in final_bosses().values() if b["map"] == map_id]
    if not bosses:
        log(f"{label}: no final boss known for map {map_id}")
        return False

    # A corpse run back in is the same run: the instance already has its words. But the core hands out the
    # lowest free instance id, so the same number comes round again (plan 62 playtest: instance 1). It is the
    # same run only if those words were written for someone in this party, and none of them has killed that
    # boss since; otherwise it is a new run, and the old words go (stale_lines) before new ones are written.
    party_json = "JSON_ARRAY(" + ",".join(map(str, guids)) + ")"
    killers = ",".join(map(str, guids))
    done = {int(r[0]) for r in sql(
        f"SELECT DISTINCT l.speaker_entry FROM dm_line l WHERE l.scene = {q(SCENE)} AND l.instance_id = {instance} "
        f"AND l.map_id = {map_id} AND l.expires_at > NOW() AND JSON_OVERLAPS(l.audience, {party_json}) "
        "AND NOT EXISTS (SELECT 1 FROM ledger_event k WHERE k.event_type = 'kill' AND k.ts > l.made_at "
        f"AND k.map_id = {map_id} AND k.actor_guid IN ({killers}) "
        "AND k.detail LIKE '{%' AND JSON_EXTRACT(k.detail, '$.entry') = l.speaker_entry)")}
    bosses = [b for b in bosses if b["entry"] not in done]
    if not bosses:
        return False

    people = rg.people(guids)
    party = [people[g] for g in guids if g in people]
    if not party:
        return False
    wants = goals([p["guid"] for p in party if p["bot"]])
    tales = tales_of([p["name"] for p in party])
    e = era()
    judge = judge or fleet.Judge()
    wrote = False
    for boss in bosses:
        met = past_meetings(boss["entry"], guids)
        runs = past_runs(boss, map_id, guids, instance)
        log(f"{label}: {boss['name']} for {', '.join(p['name'] for p in party)} (instance {instance})"
            + (f", remembers {len(met)}" if met else "")
            + (f", earlier runs: {' '.join(r['outcome'] for r in runs)}" if runs else ""))
        try:
            lines, model = write_lines(party_prompt(boss, e, party, met, tales, wants, runs), boss, e, judge,
                                       [boss["name"]] + [p["name"] for p in party], 3, "dm_party_line")
        except RuntimeError as ex:
            log(f"  gave up: {ex}")
            continue
        for line in lines:
            log(f"  {line}")
        why = "; ".join(filter(None, [
            "remembers " + ", ".join(p["name"] for p in party if p["guid"] in met) if met else "",
            "earlier runs: " + ", ".join(f"{r['outcome']} ({r['when']})" for r in runs) if runs else "",
            f"{len(tales)} tale(s) of them" if tales else "",
            f"{len(wants)} goal(s)" if wants else ""])) or "who they are"
        if dry_run:
            continue
        # An earlier run's words for this instance number. The module reads `rank`, then id, so they would
        # be spoken ahead of these.
        sql(f"UPDATE dm_line SET expires_at = NOW() WHERE scene = {q(SCENE)} AND instance_id = {instance} "
            f"AND speaker_entry = {boss['entry']} AND expires_at > NOW()", fetch=False)
        audience = party_json
        rows = ",".join(f"({q(SCENE)}, {instance}, {map_id}, {boss['entry']}, {audience}, {q(line)}, {q(why)}, {i}, "
                        f"{int(cue_id) if cue_id else 'NULL'}, {q(model)}, NOW() + INTERVAL {PARTY_LINE_HOURS} HOUR)"
                        for i, line in enumerate(lines))
        sql("INSERT INTO dm_line (scene, instance_id, map_id, speaker_entry, audience, words, why, `rank`, cue_id, "
            f"model, expires_at) VALUES {rows};", fetch=False)
        wrote = True
    return wrote


# ---------------------------------------------------------------------------
# the innkeeper

def inns():
    """{zone id: [(entry, name, place, map id)]} for every innkeeper on this era's maps, placed by the terrain files."""
    maps = ERA_MAPS.get(site.get("ERA", "classic"), ERA_MAPS["classic"])
    names = areas.load()
    out = {}
    for entry, name, map_id, x, y in world_sql(
            "SELECT DISTINCT t.entry, t.name, c.map, c.position_x, c.position_y FROM creature c "
            "JOIN creature_template t ON t.entry = c.id WHERE t.npcflag & 65536 "
            f"AND c.map IN ({','.join(map(str, sorted(maps)))})"):
        entry, map_id = int(entry), int(map_id)
        area = areas.area_at(map_id, float(x), float(y))
        zone = INN_ZONE_FIX.get(entry) or areas.zone_of(area)
        if zone:
            place = CAPITALS.get(zone) or names.get(area) or names.get(zone, "")
            out.setdefault(zone, []).append((entry, name, place, map_id))
    return out


def zone_name(zone):
    return CAPITALS.get(zone) or areas.load().get(zone, "these parts")


def rumours(zone, horde, limit=4):
    """The talk going round the zone, as this player's side hears it, newest first."""
    return [r[0] for r in sql(f"SELECT words FROM chronicle_rumour WHERE zone_id = {int(zone)} "
                              f"AND team = {1 if horde else 0} AND expires_at > NOW() ORDER BY id DESC LIMIT {limit}")]


def company(guid):
    """Who the player travels with right now (the saved group), not counting them."""
    rows = sql("SELECT m.memberGuid FROM group_member m JOIN group_member me ON me.guid = m.guid "
               f"WHERE me.memberGuid = {int(guid)} AND m.memberGuid <> {int(guid)}")
    return [int(r[0]) for r in rows]


def inn_history(guid, zone, limit=2):
    """What innkeepers in this zone said to this player before, newest first, and roughly when."""
    rows = sql("SELECT l.words, DATE(e.ts) FROM ledger_event e JOIN dm_line l "
               "ON l.id = JSON_EXTRACT(e.detail, '$.line') WHERE e.event_type = 'dm_scene' "
               f"AND JSON_UNQUOTE(JSON_EXTRACT(e.detail, '$.kind')) = {q(INN)} AND e.actor_guid = {int(guid)} "
               f"AND e.zone_id = {int(zone)} ORDER BY e.id DESC LIMIT {limit}")
    return [(w, when(d)) for w, d in rows]


INN_RULES = ("Rules: each line is one to three sentences and at most thirty words, said across the bar to them as "
             "they walk in. You are a person of this world; say nothing a person in it could not say. No numbers or "
             "figures. No words from outside the world. No stage directions, nothing in the third person about "
             "yourself. Do not name your inn or your town. Era: {setting}\n\n"
             'Reply with JSON only: {{"lines": ["...", "..."]}}')


def inn_prompt(e, zone, person, party, talk, tales, said):
    def who(p):
        return f"{p['name']}, a {rg.RACES.get(p['race'], 'wanderer')} {rg.CLASSES.get(p['cls'], 'traveller')}"
    # The zone and not the town: a contested zone has an inn for each side, and any of them may say the line.
    return (f"You keep an inn in {zone_name(zone)}. Innkeepers hear everything: travellers talk, and you pass it on.\n\n"
            + f"Coming through your door now: {who(person)}"
            + (", with " + "; ".join(who(p) for p in party) if party else "") + ".\n\n"
            + ("What is being said in these parts:\n" + "\n".join(f"- {t}" for t in talk) + "\n\n" if talk else "")
            + ("What the land says of them (you have heard it):\n" + "\n".join(f"- {t}" for t in tales) + "\n\n"
               if tales else "")
            + ("You have spoken to " + person["name"] + " before; do not say the same again:\n"
               + "\n".join(f'- {when_}: "{w}"' for w, when_ in said) + "\n\n" if said else "")
            + f"Write {INN_LINES} different things you might say to {person['name']} as they come in. Each passes on "
              "ONE piece of talk from above in your own words, as gossip, or, if the land speaks of them, lets them "
              "know their name has reached you. Address them by name only if you have heard of them or have "
              "spoken before; otherwise by what you can see. Warm or wary as suits the news.\n\n"
            + INN_RULES.format(setting=e["setting"]))


def inn_lines(guid, zone, dry_run=False, judge=None, cue_id=None, label="inn"):
    """Write an innkeeper's lines for one player in one zone. True when lines were written."""
    places = inns().get(int(zone))
    if not places:
        log(f"{label}: no innkeeper in zone {zone}")
        return False
    people = rg.people([guid] + company(guid))
    person = people.get(int(guid))
    if not person:
        return False
    party = [p for g, p in people.items() if g != int(guid)]
    horde = person["race"] in rg.HORDE
    talk = rumours(zone, horde)
    tales = tales_of([person["name"]] + [p["name"] for p in party])
    said = inn_history(guid, zone)
    e = era()
    log(f"{label}: inn lines for {person['name']} in {zone_name(zone)} ({len(talk)} rumour(s), {len(tales)} tale(s)"
        + (f", spoken to {len(said)}x before" if said else "") + ")")
    speaker = dict(entry=0, name="the innkeeper", voice=[])
    try:
        lines, model = write_lines(inn_prompt(e, zone, person, party, talk, tales, said), speaker, e,
                                   judge or fleet.Judge(), [person["name"]] + [p["name"] for p in party],
                                   INN_LINES, "dm_inn_line", stage=INN)
    except RuntimeError as ex:
        log(f"  gave up: {ex}")
        return False
    for line in lines:
        log(f"  {line}")
    if dry_run:
        return False
    why = "; ".join(filter(None, [f"{len(talk)} rumour(s)" if talk else "", f"{len(tales)} tale(s)" if tales else "",
                                  "spoken to before" if said else ""])) or "who they are"
    map_id = places[0][3]
    rows = ",".join(f"({q(INN)}, 0, {map_id}, 0, JSON_ARRAY({int(guid)}), {int(guid)}, {int(zone)}, {q(line)}, "
                    f"{q(why)}, {i}, {int(cue_id) if cue_id else 'NULL'}, {q(model)}, "
                    f"NOW() + INTERVAL {INN_LINE_HOURS} HOUR)" for i, line in enumerate(lines))
    sql("INSERT INTO dm_line (scene, instance_id, map_id, speaker_entry, audience, for_guid, zone_id, words, why, "
        f"`rank`, cue_id, model, expires_at) VALUES {rows};", fetch=False)
    return True


def inn_waiting(guid, zone):
    """Lines still waiting for this player in this zone: written, not expired, not yet spoken."""
    return int(sql(f"SELECT COUNT(*) FROM dm_line l WHERE l.scene = {q(INN)} AND l.for_guid = {int(guid)} "
                   f"AND l.zone_id = {int(zone)} AND l.expires_at > NOW() AND NOT EXISTS (SELECT 1 FROM ledger_event e "
                   "WHERE e.event_type = 'dm_scene' AND e.actor_guid = l.for_guid "
                   "AND JSON_EXTRACT(e.detail, '$.line') = l.id)")[0][0])


def answer_zone(event_id, actor, zone, judge, inn_zones):
    if zone not in inn_zones or inn_waiting(actor, zone):
        return False
    return inn_lines(actor, zone, judge=judge, cue_id=event_id, label=f"cue {event_id}")


# ---------------------------------------------------------------------------
# the dashboard snapshot (phase 1e): cues, lines written, lines spoken, misses

def _names(guids):
    return {g: p["name"] for g, p in rg.people(guids).items()} if guids else {}


def snapshot_runs(since, bosses, names, limit=25):
    """The dungeon runs of real players since `since`, newest first: each final boss, what was written for it,
    what was said, and how it ended. A boss killed with nothing said is a miss: the trigger never fired."""
    runs = {}
    for event_id, ts, actor, map_id, detail in sql(
            "SELECT id, UNIX_TIMESTAMP(ts), actor_guid, map_id, detail FROM ledger_event "
            f"WHERE event_type = 'instance_enter' AND ts >= {q(since)} ORDER BY id"):
        d = json.loads(detail or "{}")
        inst = int(d.get("instance", 0))
        if not inst or d.get("raid"):
            continue
        r = runs.setdefault(inst, dict(instance=inst, map=int(map_id), ts=int(float(ts)), last=int(float(ts)),
                                       cue=int(event_id), player=int(actor), members=set()))
        r["last"] = int(float(ts))
        r["members"] |= {int(actor)} | {int(g) for g in d.get("party", [])}
    out = []
    for r in sorted(runs.values(), key=lambda r: r["ts"], reverse=True)[:limit]:
        ids = ",".join(map(str, sorted(r["members"])))
        window = (f"ts >= FROM_UNIXTIME({r['ts']}) AND ts <= FROM_UNIXTIME({r['last']}) + INTERVAL {RUN_HOURS} HOUR "
                  f"AND map_id = {r['map']} AND actor_guid IN ({ids})")
        spoken = {}
        for line, entry, ts in sql("SELECT JSON_EXTRACT(detail, '$.line'), JSON_EXTRACT(detail, '$.entry'), "
                                   "UNIX_TIMESTAMP(ts) FROM ledger_event WHERE event_type = 'dm_scene' "
                                   f"AND JSON_EXTRACT(detail, '$.instance') = {r['instance']}"):
            spoken[int(entry)] = (int(line), int(float(ts)))
        written = {}
        for lid, entry, words, why, rank, model, made in sql(
                "SELECT id, speaker_entry, words, why, `rank`, model, UNIX_TIMESTAMP(made_at) FROM dm_line "
                f"WHERE scene = {q(SCENE)} AND instance_id = {r['instance']} ORDER BY `rank`, id"):
            written.setdefault(int(entry), []).append(dict(id=int(lid), words=words, why=why, rank=int(rank),
                                                           model=model, made=int(float(made))))
        here = [b for b in bosses.values() if b["map"] == r["map"]]
        rows = []
        for b in here:
            kills = int(sql(f"SELECT COUNT(*) FROM ledger_event WHERE event_type = 'kill' AND {window} "
                            f"AND JSON_EXTRACT(detail, '$.entry') = {b['entry']}")[0][0])
            fell = int(sql(f"SELECT COUNT(*) FROM ledger_event WHERE event_type = 'death' AND {window} "
                           f"AND JSON_EXTRACT(detail, '$.entry') = {b['entry']}")[0][0])
            said = None
            if b["entry"] in spoken:
                lid, ts = spoken[b["entry"]]
                row = sql(f"SELECT words, instance_id FROM dm_line WHERE id = {lid}")
                said = dict(id=lid, ts=ts, words=row[0][0] if row else "", fallback=bool(row) and row[0][1] == "0")
            open_ = time.time() - r["last"] < RUN_HOURS * 3600
            outcome = ("killed" if kills else "fell" if fell else "spoke" if said
                       else "under way" if open_ else "never reached")
            lines = written.get(b["entry"], [])
            rows.append(dict(entry=b["entry"], name=b["name"], outcome=outcome, said=said, lines=lines,
                             miss=bool(kills and not said),
                             latency=(lines[0]["made"] - r["ts"]) if lines else None))
        # A dungeon with several final bosses (one per Scarlet Monastery wing) only fights one per run.
        if len(rows) > 1:
            rows = [x for x in rows if x["said"] or x["lines"] or x["outcome"] in ("killed", "fell")] or rows[:1]
        out.append(dict(instance=r["instance"], map=r["map"], place=rg.lands.INSTANCE_NAMES.get(r["map"], str(r["map"])),
                        ts=r["ts"], player=names.get(r["player"], str(r["player"])),
                        party=[names.get(g, str(g)) for g in sorted(r["members"]) if g != r["player"]], bosses=rows))
    return out


def snapshot_inn(since, names, limit=30):
    """Inn lines written since `since`, newest first, grouped by the arrival that asked for them."""
    said = {int(line): int(float(ts)) for line, ts in sql(
        "SELECT JSON_EXTRACT(detail, '$.line'), UNIX_TIMESTAMP(ts) FROM ledger_event WHERE event_type = 'dm_scene' "
        f"AND JSON_UNQUOTE(JSON_EXTRACT(detail, '$.kind')) = {q(INN)} AND ts >= {q(since)}")}
    by = {}
    now = time.time()
    for lid, guid, zone, words, why, made, expires, cue in sql(
            "SELECT id, for_guid, zone_id, words, why, UNIX_TIMESTAMP(made_at), UNIX_TIMESTAMP(expires_at), "
            f"COALESCE(cue_id, 0) FROM dm_line WHERE scene = {q(INN)} AND made_at >= {q(since)} ORDER BY id"):
        key = (int(guid), int(zone), int(float(made)) // 60, int(cue))
        g = by.setdefault(key, dict(player=names.get(int(guid), str(guid)), zone=int(zone), place=zone_name(int(zone)),
                                    made=int(float(made)), why=why, lines=[]))
        g["lines"].append(dict(id=int(lid), words=words, said=said.get(int(lid)),
                               expired=bool(expires and float(expires) < now and int(lid) not in said)))
    return sorted(by.values(), key=lambda g: g["made"], reverse=True)[:limit]


def snapshot_fallback(bosses):
    spoken = {}
    for line, n in sql("SELECT JSON_EXTRACT(detail, '$.line'), COUNT(*) FROM ledger_event "
                       "WHERE event_type = 'dm_scene' GROUP BY 1"):
        spoken[int(line)] = int(n)
    out = {}
    for lid, entry, words in sql(f"SELECT id, speaker_entry, words FROM dm_line WHERE scene = {q(SCENE)} "
                                 "AND instance_id = 0 ORDER BY speaker_entry, `rank`"):
        b = bosses.get(int(entry))
        if b:
            out.setdefault(int(entry), dict(name=b["name"], place=b["place"], lines=[]))["lines"].append(
                dict(words=words, spoken=spoken.get(int(lid), 0)))
    return sorted(out.values(), key=lambda b: b["place"])


def publish():
    import datetime as dt
    since = (dt.datetime.now() - dt.timedelta(days=SNAPSHOT_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    bosses = final_bosses()
    guids = set()
    for actor, detail in sql("SELECT actor_guid, detail FROM ledger_event WHERE event_type = 'instance_enter' "
                             f"AND ts >= {q(since)}"):
        guids |= {int(actor)} | {int(g) for g in json.loads(detail or "{}").get("party", [])}
    guids |= {int(r[0]) for r in sql(f"SELECT DISTINCT for_guid FROM dm_line WHERE scene = {q(INN)} "
                                     f"AND made_at >= {q(since)}")}
    names = _names(guids)
    runs = snapshot_runs(since, bosses, names)
    inn = snapshot_inn(since, names)
    arrivals = int(sql("SELECT COUNT(*) FROM ledger_event WHERE event_type = 'zone_change' AND actor_is_bot = 0 "
                       f"AND ts >= {q(since)}")[0][0])
    boss_rows = [b for r in runs for b in r["bosses"]]
    doc = dict(
        generated=int(time.time()), days=SNAPSHOT_DAYS, paused=os.path.exists(PAUSE_FILE),
        chance=dict(boss=int(site.get("DM_BOSS_CHANCE", "100")), inn=int(site.get("DM_INN_CHANCE", "100"))),
        totals=dict(
            runs=len(runs),
            boss_written=sum(1 for b in boss_rows if b["lines"]),
            boss_spoken=sum(1 for b in boss_rows if b["said"]),
            boss_fallback=sum(1 for b in boss_rows if b["said"] and b["said"]["fallback"]),
            boss_killed=sum(1 for b in boss_rows if b["outcome"] == "killed"),
            boss_missed=sum(1 for b in boss_rows if b["miss"]),
            arrivals=arrivals,
            inn_written=sum(len(g["lines"]) for g in inn),
            inn_spoken=sum(1 for g in inn for x in g["lines"] if x["said"]),
            inn_expired=sum(1 for g in inn for x in g["lines"] if x["expired"])),
        runs=runs, inn=inn, fallback=snapshot_fallback(bosses))
    os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
    tmp = SNAPSHOT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, SNAPSHOT)
    return doc


# ---------------------------------------------------------------------------
# the loop

def cursor():
    rows = sql("SELECT last_id FROM dm_cursor WHERE source = 'ledger_event'")
    if rows:
        return int(rows[0][0])
    # First run: start at the end. The DM speaks to parties that walk in from now on, not to old runs.
    last = int(sql("SELECT COALESCE(MAX(id), 0) FROM ledger_event")[0][0])
    set_cursor(last)
    return last


def set_cursor(last_id):
    sql("INSERT INTO dm_cursor (source, last_id) VALUES ('ledger_event', "
        f"{int(last_id)}) ON DUPLICATE KEY UPDATE last_id = VALUES(last_id);", fetch=False)


def cycle(judge):
    cur = cursor()
    # Read up to a fixed ceiling, then move the cursor to it: a cue written between two queries is never
    # stepped over.
    newest = int(sql("SELECT COALESCE(MAX(id), 0) FROM ledger_event")[0][0])
    rows = sql("SELECT id, event_type, actor_guid, detail FROM ledger_event "
               f"WHERE id > {cur} AND id <= {newest} AND (event_type IN ('instance_enter', 'dm_scene') "
               "OR (event_type = 'zone_change' AND actor_is_bot = 0)) ORDER BY id LIMIT 200")
    chance = int(site.get("DM_BOSS_CHANCE", "100"))
    inn_chance = int(site.get("DM_INN_CHANCE", "100"))
    inn_zones = set(inns()) if any(r[1] == "zone_change" for r in rows) else set()
    for event_id, kind, actor, detail in rows:
        event_id = int(event_id)
        if kind == "dm_scene":
            d = json.loads(detail or "{}")
            log(f"spoken: {d.get('name')} to {actor} ({d.get('kind')}, line {d.get('line')}, instance {d.get('instance')})")
        elif kind == "zone_change":
            if random.randrange(100) < inn_chance:
                try:
                    answer_zone(event_id, int(actor), int(json.loads(detail or "{}").get("to", 0)), judge, inn_zones)
                except Exception as ex:
                    log(f"cue {event_id} failed: {ex}")
        elif random.randrange(100) < chance:
            try:
                answer_cue(event_id, judge=judge)
            except Exception as ex:   # a bad cue must not stall the cursor behind it
                log(f"cue {event_id} failed: {ex}")
        set_cursor(event_id)
    if len(rows) < 200 and newest > cur:
        set_cursor(newest)
    return len(rows)
    # Expired party lines are kept, not deleted: the module no longer loads them, and the next run at the same
    # boss quotes what was said (past_runs). Three short rows a run.


def run(args):
    ensure_schema()
    judge = fleet.Judge()
    log(f"dm: following ledger_event every {args.interval} s")
    n = 0
    while True:
        seen = 0
        if not os.path.exists(PAUSE_FILE):
            try:
                seen = cycle(judge)
            except Exception as ex:
                log(f"cycle failed: {ex}")
        if seen or n % PUBLISH_EVERY == 0:
            try:
                publish()
            except Exception as ex:
                log(f"publish failed: {ex}")
        n += 1
        time.sleep(args.interval)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("boss-words")
    b.add_argument("--map", type=int)
    b.add_argument("--force", action="store_true")
    b.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--interval", type=int, default=10)
    c = sub.add_parser("cue")
    c.add_argument("event_id", type=int)
    c.add_argument("--dry-run", action="store_true")
    t = sub.add_parser("try", help="write (but never store) a party line for any party, with no dungeon entry")
    t.add_argument("--map", type=int, required=True)
    t.add_argument("--party", required=True, help="comma-separated guids, the player first")
    sub.add_parser("bosses")
    i = sub.add_parser("inn", help="write a player's inn lines for a zone now (stored unless --dry-run)")
    i.add_argument("--guid", type=int, required=True)
    i.add_argument("--zone", type=int, help="default: where the ledger last saw them")
    i.add_argument("--dry-run", action="store_true")
    sub.add_parser("inns")
    sub.add_parser("publish")
    args = ap.parse_args()

    if args.cmd == "boss-words":
        boss_words(args)
    elif args.cmd == "run":
        run(args)
    elif args.cmd == "cue":
        answer_cue(args.event_id, dry_run=args.dry_run)
    elif args.cmd == "try":
        party_lines(args.map, 0, [int(g) for g in args.party.split(",")], None, True, None, "try")
    elif args.cmd == "bosses":
        for b in sorted(final_bosses().values(), key=lambda b: b["map"]):
            print(f"{b['map']:4} {b['entry']:6} {b['name']:32} {b['place']:28} {len(b['voice'])} stock lines")
    elif args.cmd == "inn":
        ensure_schema()
        zone = args.zone or int((sql(f"SELECT zone_id FROM ledger_event WHERE actor_guid = {args.guid} "
                                     "ORDER BY id DESC LIMIT 1") or [[0]])[0][0])
        inn_lines(args.guid, zone, dry_run=args.dry_run)
    elif args.cmd == "publish":
        d = publish()
        print(f"{SNAPSHOT}: {json.dumps(d['totals'])}")
    elif args.cmd == "inns":
        for zone, places in sorted(inns().items(), key=lambda kv: zone_name(kv[0])):
            print(f"{zone:5} {zone_name(zone):24} " + ", ".join(f"{n} ({p})" for _, n, p, _ in places))


if __name__ == "__main__":
    main()
