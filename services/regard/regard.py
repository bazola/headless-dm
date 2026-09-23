#!/usr/bin/env python3
"""Regard: every bot's running feeling toward the people it deals with.

custom wow plans/14, step A2. Reads what mod-ledger records (ledger_chat, ledger_event), turns it
into changes of feeling and keeps them in acore_characters.regard. mod-ollama-chat (local patch)
loads that table into prompts as words ("you dislike and distrust them"); the dashboard reads
regard.json. Runs outside the worldserver as a user service and never touches game objects.

Standing rule: bots are people living in Azeroth. Reasons and sentences are in-world words.

Each cycle:
  1. events    rules, no LLM: lost or won a duel, killed by someone, thrown out of a group,
               grouped up, a dungeon boss slain together.
  2. chat      lines are cut into conversations (same place and channel, no long pause) and each
               goes to the judge lane (qwen3-8b, JSON mode), which says who took what from whom.
               If the judge is unreachable the chat is left for the next cycle.
  3. decay     feelings drift back toward a baseline: guild mates warm, the other faction cool.
  4. words     when a feeling has moved far since it was last put into words, a writing lane
               writes the one sentence the bot carries ("You've no love for them since ...").
  5. snapshot  /opt/wow/server/data/dashboard-data/regard.json for the dashboard, plus
               ties/<guid>.json per person -- everyone they feel about and everyone who
               feels about them, whole, which the dashboard fetches when a Ties tab opens.

Usage:
  python3 regard.py run [--interval 120]   service loop (systemd user unit wow-regard)
  python3 regard.py once                   one cycle, then exit
  python3 regard.py show NAME              how NAME feels about people, and who feels what about NAME
Kill switch: `systemctl --user stop wow-regard`, or create /opt/wow/regard/PAUSE.
"""
import argparse
import concurrent.futures as cf
import bisect
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

# The sibling services, found from this file instead of through the /opt/wow symlinks (plan 23 W2), so a
# checkout runs wherever it is put.
_SERVICES = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(_SERVICES, "lore"))
import era as eras  # noqa: E402
import fleet  # noqa: E402
import lands  # noqa: E402
import areas  # noqa: E402  (beside this file, not in /opt/wow/lore: area names out of AreaTable.dbc)

HERE = os.path.dirname(os.path.abspath(__file__))
# Where this realm lives now comes from site/, with the live server config as the fallback (plan 23 W1).
# realpath, not HERE: HERE is the symlinked /opt/wow/regard, whose parent is /opt/wow (plan 23 W1).
sys.path.insert(0, _SERVICES)
from common import site  # noqa: E402

_DASH = os.path.join(site.get("DATA_DIR", "/opt/wow/server/data"), "dashboard-data")
SNAPSHOT = os.path.join(_DASH, "regard.json")
COMPANIES_SNAPSHOT = os.path.join(_DASH, "companies.json")
CHAT_SNAPSHOT = os.path.join(_DASH, "chat.json")
MEMORIES_SNAPSHOT = os.path.join(_DASH, "memories.json")
MEMORIES_DIR = os.path.join(_DASH, "memories")                 # one file per bot, for their own tab
MEMORIES_ARCHIVE = os.path.join(_DASH, "memories-all.json")    # the whole store, on demand
TIES_DIR = os.path.join(_DASH, "ties")                         # one file per person: every tie they have
# The whole of what the dashboard's Feelings panel otherwise shows only the head of, for the
# full-screen views that let the operator check the lot. Fetched on demand, never polled.
MOMENTS_SNAPSHOT = os.path.join(_DASH, "moments.json")
TALKS_SNAPSHOT = os.path.join(_DASH, "talks.json")
RANKED_SNAPSHOT = os.path.join(_DASH, "ranked.json")
# Plans/42: one file per character holding their whole record, and a light index beside it.
JOURNEYS_SNAPSHOT = os.path.join(_DASH, "journeys.json")
JOURNEYS_DIR = os.path.join(_DASH, "journeys")
CHAT_SNAPSHOT_LINES = 4000   # the whole party record; a week of play produced about a thousand lines
MEMORIES_SNAPSHOT_ROWS = 400   # the head of the store; the whole of it is thousands and grows daily
PAUSE_FILE = os.path.join(HERE, "PAUSE")
# The presence rate limiters, kept across restarts (plans/37 §10). In memory alone they re-fired every
# throttled moment whenever this service was restarted: cycles of 11-31 moments became 111, 120 and 88 on
# 2026-09-21, nudging some 380 regard rows that regard_log cannot see (presence commits with
# log_moments=False), which silently corrupts any measurement taken across a restart.
PRESENCE_STATE = os.path.join(HERE, "presence-state.json")

RACES = {1: "Human", 2: "Orc", 3: "Dwarf", 4: "Night Elf", 5: "Forsaken", 6: "Tauren",
         7: "Gnome", 8: "Troll", 10: "Blood Elf", 11: "Draenei"}
HORDE = {2, 5, 6, 8, 10}
CLASSES = {1: "warrior", 2: "paladin", 3: "hunter", 4: "rogue", 5: "priest", 6: "death knight",
           7: "shaman", 8: "mage", 9: "warlock", 11: "druid"}

# Tuning. Scores run -100..100; the judge's per-moment values run -10..10.
BASE_SAME_GUILD = 20.0
BASE_OTHER_FACTION = -15.0
STANCE_SHARE = 0.25          # members of two companies start this share of the companies' stance apart
RULES = {"duel_lost": -3.0, "duel_won": 1.0, "killed_by": -25.0, "kicked_by": -10.0,
         "grouped": 1.0, "boss_together": 4.0, "left_company": -10.0, "poached": -15.0,
         "declined": -3.0, "fell_beside": 2.0, "fell_careless": -1.5}
SPEAKER_SHARE = 0.6          # how far a speaker's own tone moves their feeling toward the person
DECAY_PER_DAY = 0.98         # share of the distance from baseline kept per day
DESCRIBE_AFTER = 20.0        # rewrite a bot's sentence once the feeling has moved this far
DESCRIBE_PER_CYCLE = 30
CHAT_SETTLE_SECONDS = 60     # younger lines wait: the conversation may still be going
CHAT_BATCH = 600
EVENT_BATCH = 5000
WINDOW_GAP_SECONDS = 120
WINDOW_MAX_LINES = 30
JUDGE_SLOTS = 2

# Companies (plan 14 B2): influence from members' deeds, holdings, incidents that move stances.
INFLUENCE_POINTS = {"kill": 1.0, "elite": 3.0, "rare": 8.0, "rare_elite": 10.0, "world_boss": 40.0,
                    "dungeon_boss": 20.0, "quest": 5.0}
KILL_RANK = {1: "elite", 2: "rare_elite", 3: "world_boss", 4: "rare"}   # creature_template.rank
SEAT_SHARE = 1.5             # deeds count this much more in a land where the company keeps a seat
SEAT_START = 200.0           # a seat's influence the first time it is counted
SEAT_FLOOR = 50.0            # ... and the least it decays to
INFLUENCE_KEEP_PER_WEEK = 0.5
HOLD_MIN = 100.0             # the least influence that holds a land
CONTEST_MARGIN = 0.15        # a challenger within this share of the holder contests the land
TAKE_MARGIN = 0.10           # ... and takes it once this far ahead
RECKON_SECONDS = 3600
SAME_PREY_SECONDS = 600
INCIDENT_STANCE = {"taken": -6.0, "contested": -3.0, "duel": -1.0, "same_prey": -2.0, "poached": -8.0}
POACH_MINUTES = 30           # a bot joining a company this soon after leaving another was poached from it (plan 18 P7;
                             # a P8f move's invite follows its leaving by a cycle or two)
INCIDENT_COOLDOWN_HOURS = {"duel": 6, "same_prey": 3, "words": 2}   # per pair of companies
WORDS_MIN = 4                # a judged moment between members of two companies this strong ...
WORDS_STANCE_SHARE = 0.4     # ... moves the companies' stance by this share of it
LAND = {z[0]: z for z in lands.CLASSIC_ZONES}

# Journeys (plans/42). A journey is read, not scored, so it is bounded by what a person can browse
# rather than by everything that happened: the busiest character on this realm carries about 350
# entries over eight days, and the cap only bites for a bot that has been running for months.
JOURNEY_MAX_PER_KIND = 400
JOURNEY_TALK_GAP = 300        # party lines further apart than this belong to different conversations
JOURNEY_GRIND_BUCKET = 3600   # ordinary kills are tallied per zone per hour, never listed one by one
JOURNEY_SECONDS = 600         # rebuilt on the chronicler's cadence, not the scorer's
# The deeds worth a line of their own. `kill` is handled apart (the notable ones named, the rest
# tallied), and `revive` is left out: 10,940 rows that say nothing the death before them has not.
JOURNEY_EVENTS = ("death", "quest_complete", "level_up", "group_join", "group_leave",
                  "zone_change", "loot_item", "pvp_kill")
# What lifts a kill out of the grind. Compared to 1, never to true: MySQL keeps JSON 1 and JSON true
# apart, and mod-ledger writes "boss":1 (plans/32 §11.9). COALESCE so the 76 rows with no rank at all
# fall into the tally rather than out of both halves.
NOTABLE_KILL = ("(COALESCE(JSON_EXTRACT(detail, '$.rank'), 0) <> 0 "
                "OR COALESCE(JSON_EXTRACT(detail, '$.boss'), 0) = 1)")

# ledger_chat.chat_type -> how lines are grouped into conversations
TYPE_CLASS = {1: "local", 6: "local", 10: "local", 2: "group", 3: "group", 39: "group", 40: "group",
              51: "group", 4: "guild", 5: "guild", 7: "whisper", 17: "channel"}
NAME_RE = re.compile(r"\b[A-Z][a-z]{2,11}\b")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# database (mysql CLI; credentials from site/secrets.env, else the live conf. Never printed.)

_DB = site.db("characters")
_db_lock = threading.Lock()


def sql(query, fetch=True):
    host, port, user, pw, db = _DB
    # utf8mb4 explicitly: the CLI default is latin1. The query goes in on stdin: a single argv
    # string is capped at 128 KB and batched writes exceed it.
    cmd = ["mysql", "--default-character-set=utf8mb4", "-h", host, "-P", port, "-u", user, db,
           "--batch", "--raw", "-N"]
    with _db_lock:
        out = subprocess.run(cmd, input=query, env={"MYSQL_PWD": pw, "PATH": "/usr/bin:/bin"},
                             capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    if not fetch:
        return []
    return [row.split("\t") for row in out.stdout.splitlines() if row]


ASCII_PUNCT = str.maketrans({"—": " - ", "–": "-", "‘": "'", "’": "'",
                             "“": '"', "”": '"', "…": "...", " ": " "})


def q(s):
    # The 3.3.5 client renders smart punctuation as boxes; fold it to ASCII.
    s = str(s).translate(ASCII_PUNCT)
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def cursor(source):
    rows = sql(f"SELECT last_id FROM regard_cursor WHERE source = {q(source)}")
    return int(rows[0][0]) if rows else 0


def cursor_sql(source, last_id):
    return (f"INSERT INTO regard_cursor (source, last_id) VALUES ({q(source)}, {int(last_id)}) "
            "ON DUPLICATE KEY UPDATE last_id = VALUES(last_id);")


def _person(r):
    return dict(guid=int(r[0]), name=r[1], race=int(r[2]), cls=int(r[3]), guild=int(r[4]), bot=r[5] == "1")


# The `bot` flag is 'bot' OR 'alt': an alt is a bot with lore and a bond, and everything downstream
# that asks "is this a bot" means it that way. Callers append their own WHERE, so this ends in a space.
_PERSON_SQL = ("SELECT c.guid, c.name, c.race, c.class, COALESCE(m.guildid, 0), pk.kind IN ('bot', 'alt') "
               "FROM characters c JOIN person_kind pk ON pk.guid = c.guid "
               "LEFT JOIN guild_member m ON m.guid = c.guid ")


def people(guids):
    guids = sorted({int(g) for g in guids if g})
    found = {}
    for part in chunks(guids, 1000):
        for r in sql(_PERSON_SQL + f"WHERE c.guid IN ({','.join(map(str, part))})"):
            p = _person(r)
            found[p["guid"]] = p
    return found


def people_by_name(names):
    names = sorted(set(names))
    found = {}
    for part in chunks(names, 500):
        for r in sql(_PERSON_SQL + f"WHERE c.name IN ({','.join(q(n) for n in part)})"):
            p = _person(r)
            found[p["name"].lower()] = p
    return found


RELATIONS = {}   # (lower guildid, higher guildid) -> stance; reloaded every cycle (rivalry.py seeds them)


def load_relations():
    RELATIONS.clear()
    for a, b, stance in sql("SELECT guild_a, guild_b, stance FROM guild_relation"):
        RELATIONS[(int(a), int(b))] = float(stance)


def baseline(a, b):
    if a["guild"] and a["guild"] == b["guild"]:
        return BASE_SAME_GUILD
    base = BASE_OTHER_FACTION if (a["race"] in HORDE) != (b["race"] in HORDE) else 0.0
    if a["guild"] and b["guild"]:
        base += RELATIONS.get((min(a["guild"], b["guild"]), max(a["guild"], b["guild"])), 0.0) * STANCE_SHARE
    return base


def refresh_guild_baselines():
    """Re-derive baselines between members of different companies after stances change; the score moves
    by the same amount, so a new feud is felt at once rather than over months of decay."""
    horde = ",".join(map(str, sorted(HORDE)))
    new = (f"IF((ca.race IN ({horde})) <> (cb.race IN ({horde})), {BASE_OTHER_FACTION}, 0) "
           f"+ COALESCE(gr.stance, 0) * {STANCE_SHARE}")
    rows = sql("UPDATE regard r JOIN characters ca ON ca.guid = r.bot_guid JOIN characters cb ON cb.guid = r.other_guid "
        "JOIN guild_member ma ON ma.guid = r.bot_guid "
        "JOIN guild_member mb ON mb.guid = r.other_guid AND mb.guildid <> ma.guildid "
        "LEFT JOIN guild_relation gr ON gr.guild_a = LEAST(ma.guildid, mb.guildid) "
        "AND gr.guild_b = GREATEST(ma.guildid, mb.guildid) "
        # MySQL applies SET left to right: the score line still sees the old baseline.
        f"SET r.score = GREATEST(-100, LEAST(100, r.score + ({new}) - r.baseline)), r.baseline = {new}; "
        "SELECT ROW_COUNT();")
    return int(rows[0][0]) if rows else 0


def stance_share_off_sql(guids, gid):
    """SQL for people no longer in company gid: they stop carrying its share of the stances between it and other
    companies, toward those companies' members and back (refresh_guild_baselines only reaches pairs who are both in a
    company). Feelings toward the old company's own members are left alone: those were earned together."""
    ids = ",".join(str(int(g)) for g in guids)
    if not ids or not gid:
        return ""
    gid, share = int(gid), f"gr.stance * {STANCE_SHARE}"
    return "\n".join(
        f"UPDATE regard r JOIN guild_member mo ON mo.guid = r.{them} AND mo.guildid <> {gid} "
        f"JOIN guild_relation gr ON gr.guild_a = LEAST({gid}, mo.guildid) AND gr.guild_b = GREATEST({gid}, mo.guildid) "
        f"LEFT JOIN guild_member mm ON mm.guid = r.{me} "
        f"SET r.score = GREATEST(-100, LEAST(100, r.score - {share})), r.baseline = r.baseline - {share} "
        f"WHERE r.{me} IN ({ids}) AND mm.guid IS NULL;"
        for me, them in (("bot_guid", "other_guid"), ("other_guid", "bot_guid")))


def words(score):
    # Keep in step with RegardWords() in mod-ollama-chat_sentiment.cpp.
    if score <= -60:
        return "you despise them"
    if score <= -30:
        return "you dislike and distrust them"
    if score <= -10:
        return "you are wary of them"
    if score < 10:
        return "you feel little either way about them"
    if score < 30:
        return "you are on good terms with them"
    if score < 60:
        return "you like and trust them"
    return "you would stand by them through anything"


# ---------------------------------------------------------------------------
# changes of feeling

class Changes:
    def __init__(self):
        self.items = []   # (feeler, about, delta, source, reason, ledger_id)
        self.words = []   # judged chat moments: (speaker, target, target_feels, why, line), for company incidents

    def add(self, feeler, about, delta, source, reason, ledger_id=None):
        if feeler and about and int(feeler) != int(about) and delta:
            self.items.append((int(feeler), int(about), float(delta), source, reason[:160], ledger_id))

    def guids(self):
        return {g for item in self.items for g in item[:2]}


def commit(changes, cast, extra_sql="", log_moments=True):
    """Apply changes (only bots feel) and extra_sql (cursor moves) in one transaction. log_moments=False leaves
    regard_log alone (plan 18 P8a: time spent near each other is too frequent to log moment by moment)."""
    items = [c for c in changes.items if c[0] in cast and cast[c[0]]["bot"] and c[1] in cast]
    stmts = ["START TRANSACTION;"]
    logs = []
    if items:
        keys = sorted({(f, a) for f, a, *_ in items})
        state = {}
        for part in chunks(keys, 400):
            cond = " OR ".join(f"(bot_guid = {f} AND other_guid = {a})" for f, a in part)
            for r in sql(f"SELECT bot_guid, other_guid, score, baseline FROM regard WHERE {cond}"):
                state[(int(r[0]), int(r[1]))] = [float(r[2]), float(r[3]), 0, None, None]
        for f, a, d, source, reason, lid in items:
            st = state.get((f, a))
            if st is None:
                b = baseline(cast[f], cast[a])
                st = state[(f, a)] = [b, b, 0, None, None]
            st[0] = max(-100.0, min(100.0, st[0] + d))
            st[2] += 1
            st[3], st[4] = reason, d
            logs.append((f, a, d, st[0], source, reason, lid))
        touched = [(k, v) for k, v in state.items() if v[2]]
        for part in chunks(touched, 300):
            stmts.append(
                "INSERT INTO regard (bot_guid, other_guid, score, baseline, familiarity, last_reason, last_delta) VALUES "
                + ",".join(f"({f},{a},{s:.2f},{b:.2f},{n},{q(r)},{d:.2f})" for (f, a), (s, b, n, r, d) in part)
                + " ON DUPLICATE KEY UPDATE score = VALUES(score), familiarity = familiarity + VALUES(familiarity),"
                  " last_reason = VALUES(last_reason), last_delta = VALUES(last_delta), updated_at = NOW();")
        for part in chunks(logs if log_moments else [], 300):
            stmts.append(
                "INSERT INTO regard_log (bot_guid, other_guid, delta, score, source, reason, ledger_id) VALUES "
                + ",".join(f"({f},{a},{d:.2f},{s:.2f},{q(src)},{q(r)},{lid if lid else 'NULL'})"
                           for f, a, d, s, src, r, lid in part) + ";")
    stmts.append(extra_sql)
    stmts.append("COMMIT;")
    sql("\n".join(stmts), fetch=False)
    return len(logs)


# ---------------------------------------------------------------------------
# 1. events

GROUPS = {}   # leader guid -> member guids; rebuilt from group events while the service runs


def creature_ranks(rows):
    """creature_template.rank for every foe named in this batch of deaths, in one lookup.

    A death row carries the killer's entry and name but not its rank, unlike a kill row, which the
    module fills in at the hook. One query a batch rather than one a corpse.
    """
    entries = set()
    for r in rows:
        if r[2] != "death":
            continue
        detail = "\t".join(r[4:])
        if not detail.startswith("{"):
            continue
        try:
            entry = json.loads(detail).get("entry")
        except ValueError:
            continue
        if entry:
            entries.add(int(entry))
    if not entries:
        return {}
    return {int(r[0]): int(r[1]) for r in sql(
        "SELECT entry, `rank` FROM acore_world.creature_template "
        f"WHERE entry IN ({','.join(map(str, sorted(entries)))})")}


def process_events(changes):
    cur = cursor("event")
    rows = sql("SELECT id, actor_guid, event_type, COALESCE(subject_guid, 0), COALESCE(detail, '') FROM ledger_event "
               f"WHERE id > {cur} AND (event_type IN ('duel_won', 'death', 'group_join', 'group_leave') "
               "OR (event_type = 'kill' AND detail LIKE '%\"boss\":1%')) "
               f"ORDER BY id LIMIT {EVENT_BATCH}")
    ranks = creature_ranks(rows)
    last = cur
    for r in rows:
        lid, actor, kind, subject = int(r[0]), int(r[1]), r[2], int(r[3])
        detail = "\t".join(r[4:])
        last = lid
        try:
            d = json.loads(detail) if detail.startswith("{") else {}
        except ValueError:
            d = {}
        if kind == "duel_won" and subject:
            changes.add(subject, actor, RULES["duel_lost"], "event", "lost a duel to them", lid)
            changes.add(actor, subject, RULES["duel_won"], "event", "beat them in a duel", lid)
        elif kind == "death" and subject:
            changes.add(actor, subject, RULES["killed_by"], "event", "they killed you", lid)
        elif kind == "death":
            # Nobody to blame: a beast, a fall, deep water. The feeling belongs to whoever was
            # standing there. Going down to something fearsome draws a group together; dying to a
            # wolf, or to the ground itself, costs a little standing with the people who watched.
            group = next((m for m in GROUPS.values() if actor in m), set())
            others = sorted(g for g in group if g != actor)
            if not others:
                continue
            foe = str(d.get("name") or "").strip()[:60]
            if ranks.get(int(d.get("entry") or 0), 0) > 0:
                for o in others:
                    changes.add(actor, o, RULES["fell_beside"], "event",
                                f"they stood with you when {foe or 'it'} brought you down", lid)
                    changes.add(o, actor, RULES["fell_beside"], "event",
                                f"you saw {foe or 'it'} bring them down beside you", lid)
            else:
                why = (f"you watched {foe} finish them off" if foe
                       else "you watched them fall with no enemy near")
                for o in others:
                    changes.add(o, actor, RULES["fell_careless"], "event", why, lid)
        elif kind == "group_join":
            leader = subject or actor
            GROUPS.setdefault(leader, {leader}).add(actor)
            if actor != leader:
                changes.add(actor, leader, RULES["grouped"], "event", "took you into their company", lid)
                changes.add(leader, actor, RULES["grouped"], "event", "joined your company", lid)
        elif kind == "group_leave":
            for members in GROUPS.values():
                members.discard(actor)
            if d.get("disband"):
                GROUPS.pop(actor, None)
            kicker = int(d.get("kicker") or 0)
            if kicker and kicker != actor and d.get("method") == 1:
                changes.add(actor, kicker, RULES["kicked_by"], "event", "threw you out of their company", lid)
        elif kind == "kill":
            group = next((m for m in GROUPS.values() if actor in m), {actor})
            name = str(d.get("name") or "a great foe")[:60]
            for a in group:
                for b in group:
                    if a != b:
                        changes.add(a, b, RULES["boss_together"], "event", f"slew {name} together", lid)
    return last, len(rows)


# Plan 18 P2: mod-playerbots writes company_answer when an unguilded bot answers a real player's charter or
# company invite. Being asked is a small overture whatever the answer; asking the same again soon adds nothing.
ANSWER_DELTA = {True: 3.0, False: 0.5}
ANSWER_REPEAT_HOURS = 6
ANSWER_ASKED = {("sign", True): "put your name to their charter", ("sign", False): "would not put your name to their charter",
                ("join", True): "joined their company at their asking", ("join", False): "would not join their company"}
ANSWER_WHY = {"trusts_them": "you trust them", "friend_inside": "a friend of yours already rides with them",
              "stranger": "you do not know them", "hardly_knows": "you hardly know them",
              "not_enough": "you do not trust them enough yet", "dislikes": "you have no liking for them",
              "enemy_inside": "someone you cannot abide is one of them",
              "in_company": "you already stand with a company", "invited": "another company had already asked for you",
              "unwilling": "you were not willing",
              # plan 18 P7: a guilded bot weighing whether to leave its company for theirs
              "defects": "you have lost faith in your own company and think well of them",
              "loyal": "you do not think well enough of them to leave your company",
              "attached": "you are bound to the people of your own company", "leads": "you lead your own company",
              "needed": "your company cannot spare you", "stays": "you cannot bring yourself to leave your company yet"}


# Plan 18 P6: a bot that turns a real player down says why, in its own words (a whisper action).
ANSWER_VOICE = {"sign": ("put your name to their charter", "put my name to your charter"),
                "join": ("join their company", "join your company")}
VOICED_PER_CYCLE = 10


def process_answers(changes, voiced=None):
    """Answers become reasons; each declined ask not repeated lately is also voiced (appended to voiced as a whisper
    action)."""
    cur = cursor("answer")
    rows = sql("SELECT a.id, a.bot_guid, a.player_guid, a.kind, a.accepted, a.reason, EXISTS(SELECT 1 FROM company_answer p "
               "WHERE p.bot_guid = a.bot_guid AND p.player_guid = a.player_guid AND p.kind = a.kind AND p.id < a.id "
               f"AND p.ts > a.ts - INTERVAL {ANSWER_REPEAT_HOURS} HOUR), COALESCE(a.guildid, 0), COALESCE(c.name, '') "
               "FROM company_answer a LEFT JOIN characters c ON c.guid = a.player_guid "
               f"WHERE a.id > {cur} ORDER BY a.id LIMIT {EVENT_BATCH}")
    last = cur
    for r in rows:
        last = int(r[0])
        bot, player, kind, accepted, reason, repeat = int(r[1]), int(r[2]), r[3], r[4] == "1", r[5], r[6] == "1"
        gid, name = int(r[7]), "\t".join(r[8:])
        if repeat or (kind, accepted) not in ANSWER_ASKED:
            continue
        why = ANSWER_WHY.get(reason, "you were not willing")
        changes.add(bot, player, ANSWER_DELTA[accepted], "answer", f"{ANSWER_ASKED[(kind, accepted)]}: {why}")
        if not accepted and voiced is not None and name and kind in ANSWER_VOICE and len(voiced) < VOICED_PER_CYCLE:
            asked, refuse = ANSWER_VOICE[kind]
            voiced.append(dict(guild=gid, player=player, name=name, kind="whisper", rank=None, prefer=bot, near=0,
                               asked=asked, refuse=refuse, why=f", because {why}"))
    return last, len(rows)


def process_membership(changes, incidents, names, extra=None):
    """Plan 18 P3: a real player joining, leaving or being cast out of a company (ledger guild_join / guild_leave,
    plan 18 P1) is an incident; leaving costs regard with every member who knew them. SQL that belongs with the cursor
    move (a leaver's stance shares, a declined invite) is appended to extra, or run at once without it."""
    cur = cursor("guild")
    stmts = []
    rows = sql("SELECT id, actor_guid, event_type, COALESCE(detail, '') FROM ledger_event "
               f"WHERE id > {cur} AND event_type IN ('guild_join', 'guild_leave', 'guild_rank', 'guild_decline') ORDER BY id LIMIT {EVENT_BATCH}")
    last = cur
    parsed = []
    for r in rows:
        last = int(r[0])
        detail = "\t".join(r[3:])
        try:
            d = json.loads(detail) if detail.startswith("{") else {}
        except ValueError:
            d = {}
        parsed.append((last, int(r[1]), r[2], d))
    cast = people({actor for _, actor, _, _ in parsed})
    titles = {(int(r[0]), int(r[1])): "\t".join(r[2:]) for r in sql("SELECT guildid, rid, rname FROM guild_rank")} \
        if any(kind == "guild_rank" for _, _, kind, _ in parsed) else {}
    for lid, actor, kind, d in parsed:
        p, gid = cast.get(actor), int(d.get("guild") or 0)
        if not p or not gid:
            continue
        company = names.get(gid, "a company")
        if p["bot"]:
            # Plan 18 P6-P8: a bot's joining, leaving or being cast out is told for every company (bot guild membership
            # only changes through these plans since P0). A bot that left another company just before joining was
            # poached from it: the companies' stance suffers and its old companions think less of it.
            if kind == "guild_join":
                came = sql(f"SELECT COALESCE(detail, '') FROM ledger_event WHERE actor_guid = {actor} "
                           f"AND event_type = 'guild_leave' AND id < {lid} AND ts >= (SELECT ts FROM ledger_event "
                           f"WHERE id = {lid}) - INTERVAL {POACH_MINUTES} MINUTE ORDER BY id DESC LIMIT 1")
                try:
                    left = json.loads(came[0][0]) if came and came[0][0].startswith("{") else {}
                except ValueError:
                    left = {}
                old = int(left.get("guild") or 0) if not left.get("removed") else 0
                if old and old != gid:
                    incidents.add(gid, old, 0, "poached", f"{p['name']} left {names.get(old, 'another company')} for {company}",
                                  INCIDENT_STANCE["poached"])
                    for r in sql(f"SELECT guid FROM guild_member WHERE guildid = {old}"):
                        changes.add(int(r[0]), actor, RULES["poached"], "event", f"left your company for {company}", lid)
                else:
                    incidents.add(gid, 0, 0, "joined", f"{p['name']} was taken into {company}")
            elif kind == "guild_leave":
                stmts.append(stance_share_off_sql([actor], gid))
                if d.get("removed"):
                    incidents.add(gid, 0, 0, "cast_out", f"{company} cast out {p['name']}")
                else:
                    incidents.add(gid, 0, 0, "left", f"{p['name']} left {company}")
            continue
        if kind == "guild_decline":
            # Plan 18 P4 gap: the player turned down an officer's invite at the dialog (mod-ledger reads the decline
            # packet). The officer who asked remembers it, and the company waits before asking again.
            asked = sql(f"SELECT id, COALESCE(actor_guid, 0) FROM company_action WHERE guildid = {gid} "
                        f"AND player_guid = {actor} AND action = 'invite' AND result = 'sent' "
                        "AND done_at > NOW() - INTERVAL 1 DAY ORDER BY id DESC LIMIT 1")
            if asked:
                stmts.append(f"UPDATE company_action SET result = 'declined' WHERE id = {int(asked[0][0])};")
                changes.add(int(asked[0][1]), actor, RULES["declined"], "event",
                            f"they turned down your offer to join {company}", lid)
            continue
        if kind == "guild_join":
            incidents.add(gid, 0, 0, "joined", f"{p['name']} was taken into {company}")
        elif kind == "guild_rank":   # plan 18 P4: the rank ladder's raises and lowerings
            verb = "raised" if d.get("promote") else "lowered"
            incidents.add(gid, 0, 0, verb,
                          f"{company} {verb} {p['name']} to {titles.get((gid, int(d.get('rank', -1))), 'a new standing')}")
        elif d.get("removed"):
            stmts.append(stance_share_off_sql([actor], gid))
            incidents.add(gid, 0, 0, "cast_out", f"{company} cast out {p['name']}")
        else:
            stmts.append(stance_share_off_sql([actor], gid))
            incidents.add(gid, 0, 0, "left", f"{p['name']} left {company}")
            for r in sql("SELECT r.bot_guid FROM regard r JOIN guild_member m ON m.guid = r.bot_guid "
                         f"AND m.guildid = {gid} WHERE r.other_guid = {actor}"):
                changes.add(int(r[0]), actor, RULES["left_company"], "event", f"turned their back on {company}", lid)
    stmts = [s for s in stmts if s]
    if extra is not None:
        extra.extend(stmts)
    elif stmts:
        sql("\n".join(stmts), fetch=False)
    return last, len(rows)


# Plan 18 P3: candidacy. How far a company has come toward taking a real player in.
CANDIDACY_OFF = os.path.join(HERE, "NO_CANDIDACY")   # kill switch: no stages, no sponsor clauses
VOUCH_REGARD = 40.0          # a member who thinks this well of the player ...
VOUCH_FAMILIARITY = 3        # ... and knows them this well vouches for them
VOUCHERS_NEEDED = 3          # vouchers (one an officer or the leader) before the company speaks for them
BLACKBALL_REGARD = -40.0     # an officer or the leader this set against them stops it
OFFER_WARMTH = 4.0           # a judged moment this warm with a member, once spoken for, makes it an offer
RANK_WEIGHT = {0: 3.0, 1: 2.0, 2: 1.5, 3: 1.0}   # guild_member.rank; lower ranks count 0.5
CANDIDACY_ASIDE = {
    "spoken_for": "You would gladly see them sworn to {company}; if the talk turns that way, tell them so.",
    "offered": "You have as good as asked them to join {company}, and they seemed glad of it; "
               "when next you meet, you mean to take them in.",
}


def candidacy(chat, names):
    """Rewrites company_candidacy and the sponsors' regard_aside rows. Returns {stage: count}, or None when off."""
    if os.path.exists(CANDIDACY_OFF):
        sql("DELETE FROM regard_aside WHERE source = 'candidacy'; DELETE FROM company_candidacy;", fetch=False)
        return None
    horde = ",".join(map(str, sorted(HORDE)))
    players = {int(r[0]): dict(name=r[1], horde=int(r[2]) in HORDE, guild=int(r[3])) for r in sql(
        "SELECT c.guid, c.name, c.race, COALESCE(m.guildid, 0) FROM characters c JOIN person_kind pk ON pk.guid = c.guid "
        "LEFT JOIN guild_member m ON m.guid = c.guid WHERE pk.kind IN ('main', 'player')")}
    horde_company = {int(g): 2 * int(h) > int(n) for g, h, n in sql(
        f"SELECT m.guildid, SUM(c.race IN ({horde})), COUNT(*) FROM guild_member m JOIN characters c ON c.guid = m.guid "
        "GROUP BY m.guildid")}
    old = {(int(r[0]), int(r[1])): (r[2], float(r[3])) for r in sql(
        "SELECT guildid, player_guid, stage, UNIX_TIMESTAMP(since) FROM company_candidacy")}
    known = {}   # (guild, player) -> [(bot, rank, score, familiarity)]
    if players:
        for r in sql("SELECT r.bot_guid, m.guildid, m.`rank`, r.other_guid, r.score, r.familiarity FROM regard r "
                     f"JOIN guild_member m ON m.guid = r.bot_guid WHERE r.other_guid IN ({','.join(map(str, players))})"):
            known.setdefault((int(r[1]), int(r[3])), []).append((int(r[0]), int(r[2]), float(r[4]), int(r[5])))
    warm = {}    # (guild, player) -> time of the latest warm moment with one of its members
    for speaker, target, feels, why, line in chat.words:
        if not speaker["bot"] and target["bot"] and target["guild"] and feels >= OFFER_WARMTH:
            key = (target["guild"], speaker["guid"])
            warm[key] = max(warm.get(key, 0.0), line["ts"])

    now = time.time()
    rows, asides, counts = [], [], {}
    for pg, p in players.items():
        for gid in names:
            members = known.get((gid, pg), [])
            vouchers = sorted((m for m in members if m[2] >= VOUCH_REGARD and m[3] >= VOUCH_FAMILIARITY),
                              key=lambda m: (m[1], -m[2]))
            if p["guild"] == gid:
                stage, sponsor, blackball = "joined", None, None
            elif not vouchers:
                continue
            else:
                sponsor = vouchers[0]
                blackball = next((m for m in sorted(members, key=lambda m: m[2])
                                  if m[1] <= 1 and m[2] <= BLACKBALL_REGARD), None)
                eligible = (len(vouchers) >= VOUCHERS_NEEDED and sponsor[1] <= 1 and not blackball and not p["guild"]
                            and horde_company.get(gid) == p["horde"])
                prev_stage, prev_since = old.get((gid, pg), ("", 0.0))
                if not eligible:
                    stage = "noticed"
                elif prev_stage == "offered" or (prev_stage == "spoken_for" and warm.get((gid, pg), 0.0) > prev_since):
                    stage = "offered"
                else:
                    stage = "spoken_for"
            prev_stage, prev_since = old.get((gid, pg), ("", 0.0))
            since = prev_since if prev_stage == stage else now
            weights = [RANK_WEIGHT.get(m[1], 0.5) for m in members]
            standing = sum(m[2] * w for m, w in zip(members, weights)) / sum(weights) if members else 0.0
            sponsor_guid = sponsor[0] if sponsor and stage in CANDIDACY_ASIDE else None
            rows.append((gid, pg, stage, sponsor_guid, len(vouchers), standing, blackball[0] if blackball else None, since))
            if sponsor_guid:
                asides.append((sponsor_guid, pg, CANDIDACY_ASIDE[stage].format(company=names[gid])))
            counts[stage] = counts.get(stage, 0) + 1

    null = lambda v: "NULL" if v is None else str(v)
    stmts = ["START TRANSACTION;", "DELETE FROM company_candidacy;", "DELETE FROM regard_aside WHERE source = 'candidacy';"]
    for part in chunks(rows, 300):
        stmts.append("INSERT INTO company_candidacy (guildid, player_guid, stage, sponsor_guid, vouchers, standing, "
                     "blackball_guid, since) VALUES " + ",".join(
                         f"({g},{pl},{q(s)},{null(sp)},{v},{st:.2f},{null(b)},FROM_UNIXTIME({int(si)}))"
                         for g, pl, s, sp, v, st, b, si in part) + ";")
    for part in chunks(asides, 300):
        # A clause from another source keeps its row: candidacy only speaks where nothing else does.
        stmts.append("INSERT IGNORE INTO regard_aside (bot_guid, other_guid, source, words) VALUES "
                     + ",".join(f"({b},{o},'candidacy',{q(w)})" for b, o, w in part) + ";")
    stmts.append("COMMIT;")
    sql("\n".join(stmts), fetch=False)
    return counts


# ---------------------------------------------------------------------------
# 0. company lifecycle (plan 18 P4b). A company is (guildid, founded_at): the core hands out MAX(guildId) + 1 at
# startup, so a dead company's id can go to a newcomer. Every cycle, before any other step, a registry row whose guild
# is gone or has another createdate is a dead company: its rows move to company_archive, its neighbours hear of it,
# and its last members carry a line about it. Nothing keyed by guild id then ever points at the dead.

# (table, guild id column, time column): when the id already belongs to a newcomer, rows newer than it are its own.
COMPANY_TABLES = [("lore_guild", "guildid", None), ("guild_seat", "guildid", None), ("guild_influence", "guildid", None),
                  ("guild_words", "guildid", None), ("company_candidacy", "guildid", None),
                  ("company_answer", "guildid", "ts"), ("guild_holding", "holder", None),
                  ("guild_relation", "guild_a", None), ("guild_relation", "guild_b", None),
                  ("guild_incident", "guild_a", "ts"), ("guild_incident", "guild_b", "ts"),
                  ("company_action", "guildid", "created_at"), ("company_rank", "guildid", None),
                  ("company_deeds", "guildid", None), ("company_rank_name", "guildid", None),
                  ("company_founding", "guildid", None), ("company_loyalty", "guildid", None),
                  ("company_churn", "from_guild", "ts"), ("company_churn", "to_guild", "ts")]
ENDED_WORDS = {"rival": "{name} is no more; {other} have one rival fewer",
               "friend": "{name} is no more; {other} have lost a friend",
               "neighbour": "{name} is no more; its banner has been taken down"}
_COLUMNS = {}


def table_columns(table):
    if table not in _COLUMNS:
        _COLUMNS[table] = [r[0] for r in sql("SELECT column_name FROM information_schema.columns WHERE table_schema = "
                                             f"DATABASE() AND table_name = {q(table)} ORDER BY ordinal_position")]
    return _COLUMNS[table]


def dead_companies(living, registry, referenced):
    """living and registry: {guildid: founded_at}; referenced: guild ids found in the company tables.
    Returns [(guildid, founded_at of the newcomer now holding the id, or None)], registry deaths first."""
    dead = [(g, living[g] if g in living else None) for g, founded in sorted(registry.items())
            if living.get(g) != founded]
    # Orphans: rows left by a company the registry never saw (it died before P4b, or while the service was away).
    dead += [(g, None) for g in sorted(referenced - set(registry) - set(living))]
    return dead


def end_company(gid, reg, newcomer_founded):
    """Archive one dead company. reg is its registry row, or None for orphaned rows. Returns its cause."""
    pattern = '{"guild":%d,%%' % gid
    since = f" AND ts >= FROM_UNIXTIME({reg['founded']})" if reg else ""
    ts = float(sql("SELECT COALESCE(UNIX_TIMESTAMP(MAX(ts)), 0) FROM ledger_event WHERE event_type = 'guild_disband' "
                   f"AND detail LIKE {q(pattern)}{since}")[0][0])
    cause = "disbanded" if ts else ("vanished" if reg else "orphaned")
    name = reg["name"] if reg else ""
    members = reg["members"] if reg else []
    cast = people(m[0] for m in members)
    roll = [[g, rank, cast[g]["name"] if g in cast else ""] for g, rank in members]
    living = company_names()

    neighbours = {}   # living guild id -> stance with the dead company
    for other, stance in sql(f"SELECT IF(guild_a = {gid}, guild_b, guild_a), stance FROM guild_relation "
                             f"WHERE {gid} IN (guild_a, guild_b)"):
        neighbours[int(other)] = float(stance)
    for (other,) in sql(f"SELECT DISTINCT s2.guildid FROM guild_seat s1 JOIN guild_seat s2 ON s2.zone_id = s1.zone_id "
                        f"AND s2.guildid <> s1.guildid WHERE s1.guildid = {gid}"):
        neighbours.setdefault(int(other), 0.0)
    neighbours = {g: s for g, s in neighbours.items() if g in living and g != gid}

    stmts = ["START TRANSACTION;",
             "INSERT INTO company_ended (guildid, founded_at, name, leader_guid, members, ended_at, cause) VALUES "
             f"({gid}, {reg['founded'] if reg else 'NULL'}, {q(name)}, {reg['leader'] if reg else 'NULL'}, "
             f"{q(json.dumps(roll))}, {f'FROM_UNIXTIME({ts})' if ts else 'NOW()'}, {q(cause)});",
             "SET @ended = LAST_INSERT_ID();"]
    # Before the relations are archived: the last members stop carrying the dead company's stance shares.
    stmts.append(stance_share_off_sql([g for g, _, _ in roll], gid))
    for table, column, time_column in COMPANY_TABLES:
        columns = table_columns(table)
        if not columns:
            continue
        cond = f"`{column}` = {gid}"
        if newcomer_founded and time_column:
            cond += f" AND `{time_column}` < FROM_UNIXTIME({newcomer_founded})"
        row = "JSON_OBJECT(" + ", ".join(f"'{c}', `{c}`" for c in columns) + ")"
        stmts += [f"INSERT INTO company_archive (ended_id, source, data) SELECT @ended, '{table}', {row} FROM {table} WHERE {cond};",
                  f"DELETE FROM {table} WHERE {cond};"]
    stmts += [f"UPDATE guild_holding SET challenger = NULL WHERE challenger = {gid};",
              # Bios keep their text; the id they were written under goes (the roll in company_ended keeps it).
              f"UPDATE lore_character SET guildid = NULL WHERE guildid = {gid}"
              + (f" AND generated_at < FROM_UNIXTIME({newcomer_founded});" if newcomer_founded else ";")]
    if reg:
        stmts.append(f"DELETE FROM company_registry WHERE guildid = {gid} AND founded_at = {reg['founded']};")
    if name:
        for other, stance in sorted(neighbours.items()):
            kind = "rival" if stance <= -30 else "friend" if stance >= 30 else "neighbour"
            stmts.append("INSERT INTO guild_incident (guild_a, guild_b, zone_id, kind, detail) VALUES "
                         f"({other}, NULL, NULL, 'disbanded', {q(ENDED_WORDS[kind].format(name=name, other=living[other])[:255])});")
        notes = [(g, f"You led {name} until the company broke apart." if reg and g == reg["leader"]
                  else f"You rode with {name} until the company broke apart.") for g, _, n in roll if n]
        for part in chunks(notes, 200):
            stmts.append("INSERT INTO lore_character_note (guid, source, note) VALUES "
                         + ",".join(f"({g}, 'lifecycle', {q(note)})" for g, note in part) + ";")
    stmts.append("COMMIT;")
    sql("\n".join(stmts), fetch=False)
    log(f"company ended: {name or 'unnamed'} (id {gid}, {cause}, {len(roll)} last members, {len(neighbours)} neighbours told)")
    return cause


def lifecycle():
    """Returns (companies newly registered, companies ended). The first run registers the living silently."""
    living = {int(r[0]): dict(founded=int(r[1]), leader=int(r[2]), name="\t".join(r[3:]))
              for r in sql("SELECT guildid, createdate, leaderguid, name FROM guild")}
    registry = {int(r[0]): dict(founded=int(r[1]), leader=int(r[2]), members=json.loads(r[3]), name="\t".join(r[4:]))
                for r in sql("SELECT guildid, founded_at, leader_guid, members, name FROM company_registry")}
    referenced = {int(r[0]) for r in sql(" UNION ".join(
        f"SELECT `{column}` FROM {table} WHERE `{column}` IS NOT NULL AND `{column}` <> 0"
        for table, column, _ in COMPANY_TABLES + [("guild_holding", "challenger", None)]))}
    dead = dead_companies({g: c["founded"] for g, c in living.items()}, {g: c["founded"] for g, c in registry.items()},
                          referenced)
    for gid, newcomer_founded in dead:
        end_company(gid, registry.get(gid), newcomer_founded)

    roster = {}
    for gid, guid, rank in sql("SELECT guildid, guid, `rank` FROM guild_member ORDER BY guildid, `rank`, guid"):
        roster.setdefault(int(gid), []).append([int(guid), int(rank)])
    fresh = [g for g in living if g not in registry or living[g]["founded"] != registry[g]["founded"]]
    stmts = ["START TRANSACTION;"]
    for part in chunks(sorted(living.items()), 200):
        stmts.append("INSERT INTO company_registry (guildid, founded_at, name, leader_guid, members) VALUES "
                     + ",".join(f"({g},{c['founded']},{q(c['name'])},{c['leader']},{q(json.dumps(roster.get(g, [])))})"
                                for g, c in part)
                     + " ON DUPLICATE KEY UPDATE founded_at = VALUES(founded_at), name = VALUES(name), "
                       "leader_guid = VALUES(leader_guid), members = VALUES(members), seen_at = NOW();")
    stmts.append("COMMIT;")
    sql("\n".join(stmts), fetch=False)
    for g in fresh:
        if registry:
            log(f"company registered: {living[g]['name']} (id {g})")
    return (len(fresh) if registry else 0), len(dead)


# ---------------------------------------------------------------------------
# 8. company actions (plan 18 P4): invites for offered candidacies and the hourly rank ladder. regard.py decides and
# writes company_action; mod-playerbots (local patch, AiPlayerbot.CompanyActions) has an officer bot of the company
# carry each out through its own session, face to face while near_only is set, and marks the row done.

ACTIONS_OFF = os.path.join(HERE, "NO_COMPANY_ACTIONS")   # kill switch: no new actions, pending ones withdrawn
LADDER_SECONDS = 3600
RANK_LEADER, RANK_OFFICER, RANK_VETERAN, RANK_MEMBER, RANK_INITIATE = 0, 1, 2, 3, 4
MEMBER_FRIENDS, MEMBER_FRIEND_REGARD, MEMBER_DAYS = 5, 30.0, 2
VETERAN_STANDING, VETERAN_OFFICER_REGARD, VETERAN_INFLUENCE = 45.0, 50.0, 200.0
OFFICER_LEADER_REGARD, OFFICER_OFFICERS_REGARD, OFFICER_INFLUENCE, OFFICER_CAP = 70.0, 50.0, 1000.0, 5
LADDER_SLACK = 15.0            # a rank is kept while its regard needs, less this, are met ...
DEMOTE_AFTER_HOURS = 24        # ... and lost after failing them this long
CAST_OUT_REGARD = -60.0        # the leader this set against a member casts them out; so does killing a member
INVITE_RETRY_HOURS = 24        # an invite not taken up is carried again after this long
DECLINE_RETRY_HOURS = 72       # an invite turned down at the dialog (ledger guild_decline) waits this long
ACTION_EXPIRE_HOURS = {"invite": 24, "promote": 96, "demote": 96, "remove": 96, "leave": 48, "whisper": 1}
PROMOTE_NEAR_HOURS = 48        # a raise waits this long to be given face to face, then is sent by word

ACTION_PROMPT = """You are one of those who lead {company}, a company in Azeroth. {charter}
{situation}
Write the one thing you say to {player} as it is done, at most 25 words. Plain words in your own voice, no quotes, no numbers, nothing outside the world."""
# Plan 18 P6: leave and whisper are spoken by the bot itself (company_action.prefer_guid), not by the company.
ACTION_OWN_PROMPT = """You are {speaker}, {speaker_desc} living in Azeroth.
{situation}
Write the one thing you say to {player}, at most 25 words. Plain words in your own voice, no quotes, no numbers, nothing outside the world."""
OWN_VOICE = {"leave", "whisper"}
ACTION_SITUATION = {
    "invite": "Your people have come to trust {player}, who is not yet one of you, and you are asking them to join the company.",
    "promote": "The company has decided to raise {player}, one of its own, to the standing of {rank}.",
    "demote": "The company's trust in {player}, one of its own, has soured, and you are lowering them to the standing of {rank}.",
    "remove": "The company is casting {player} out for good.",
    "leave": "You have ridden with {company}, which {player} leads, but you have come to think badly of how {player} leads it{why}. You are leaving the company and telling {player} why.",
    "whisper": "{player} asked you to {asked}, and you are turning them down{why}.",
}
ACTION_FALLBACK = {
    "invite": "You have been spoken for among us, {player}. Ride with {company}, if you will have us.",
    "promote": "Our people speak well of you, {player}. From this day you stand as {rank} of {company}.",
    "demote": "Word among us has soured, {player}. You stand as {rank} of {company} until you mend it.",
    "remove": "You are no longer one of {company}, {player}. Do not look for welcome among us.",
    "leave": "I am done following you, {player}. {company} will have to do without me.",
    "whisper": "Not this time, {player}. I will not {asked}.",
}


def meets(rank, s, slack=0.0):
    """Whether a player's standing in their company meets the needs of rank; slack lowers the regard needs only."""
    if rank >= RANK_INITIATE:
        return True
    if sum(1 for v in s["scores"] if v >= MEMBER_FRIEND_REGARD - slack) < MEMBER_FRIENDS or s["days"] < MEMBER_DAYS:
        return False
    if rank == RANK_MEMBER:
        return True
    if (s["standing"] < VETERAN_STANDING - slack or s["best_officer"] < VETERAN_OFFICER_REGARD - slack
            or s["influence"] < VETERAN_INFLUENCE):
        return False
    if rank == RANK_VETERAN:
        return True
    warm = sum(1 for v in s["officer_scores"] if v >= OFFICER_OFFICERS_REGARD - slack)
    return (s["leader"] >= OFFICER_LEADER_REGARD - slack and s["influence"] >= OFFICER_INFLUENCE
            and (s["officers"] == 0 or 2 * warm > s["officers"]))


def ladder_step(rank, s, officers_in_company, failing, now, cast_out):
    """One player's hourly check. Returns (action or None, new rank or None, failing_since)."""
    if cast_out:
        return "remove", None, 0.0
    best = next((k for k in (RANK_OFFICER, RANK_VETERAN, RANK_MEMBER)
                 if meets(k, s) and (k != RANK_OFFICER or officers_in_company < OFFICER_CAP)), RANK_INITIATE)
    if best < rank:
        return "promote", rank - 1, 0.0
    if rank < RANK_INITIATE and not meets(rank, s, LADDER_SLACK):
        failing = failing or float(now)
        if now - failing >= DEMOTE_AFTER_HOURS * 3600:
            return "demote", rank + 1, 0.0
        return None, None, failing
    return None, None, 0.0


def ladder():
    """Hourly: raise, lower or cast out the real players in companies. Returns new actions, or [] when not due."""
    now = int(time.time())
    if now - cursor("ladder") < LADDER_SECONDS:
        return []
    sql(cursor_sql("ladder", now), fetch=False)
    members = sql("SELECT m.guildid, m.guid, m.`rank`, c.name FROM guild_member m JOIN characters c ON c.guid = m.guid "
                  "JOIN person_kind pk ON pk.guid = c.guid WHERE pk.kind IN ('main', 'player')")
    pending = {(int(r[0]), int(r[1])) for r in sql("SELECT guildid, player_guid FROM company_action WHERE done_at IS NULL")}
    failing_since = {(int(r[0]), int(r[1])): float(r[2]) for r in sql(
        "SELECT guildid, player_guid, COALESCE(UNIX_TIMESTAMP(failing_since), 0) FROM company_rank")}
    officers = {int(r[0]): int(r[1]) for r in sql("SELECT guildid, COUNT(*) FROM guild_member WHERE `rank` = 1 GROUP BY guildid")}
    deeds = {(int(r[0]), int(r[1])): float(r[2]) for r in sql("SELECT guildid, player_guid, influence FROM company_deeds")}
    actions, rows = [], []
    for r in members:
        gid, pg, rank, name = int(r[0]), int(r[1]), int(r[2]), "\t".join(r[3:])
        if rank == RANK_LEADER:
            continue
        pattern = '{"guild":%d,%%' % gid
        joined = float(sql("SELECT COALESCE(UNIX_TIMESTAMP(MAX(ts)), 0) FROM ledger_event WHERE event_type = 'guild_join' "
                           f"AND actor_guid = {pg} AND detail LIKE {q(pattern)}")[0][0])
        if not joined:   # joined before the ledger kept guild events
            since = sql(f"SELECT UNIX_TIMESTAMP(since) FROM company_candidacy WHERE guildid = {gid} AND player_guid = {pg} "
                        "AND stage = 'joined'")
            joined = float(since[0][0]) if since else float(now)
        feel = [(int(x[0]), float(x[1])) for x in sql(
            "SELECT m.`rank`, r.score FROM regard r JOIN guild_member m ON m.guid = r.bot_guid "
            f"AND m.guildid = {gid} WHERE r.other_guid = {pg}")]
        weights = [RANK_WEIGHT.get(k, 0.5) for k, _ in feel]
        s = dict(scores=[v for _, v in feel], days=(now - joined) / 86400.0,
                 standing=sum(v * w for (_, v), w in zip(feel, weights)) / sum(weights) if feel else 0.0,
                 best_officer=max([v for k, v in feel if k <= RANK_OFFICER], default=0.0),
                 leader=max([v for k, v in feel if k == RANK_LEADER], default=0.0),
                 officer_scores=[v for k, v in feel if k == RANK_OFFICER],
                 officers=officers.get(gid, 0) - (1 if rank == RANK_OFFICER else 0),
                 influence=deeds.get((gid, pg), 0.0))
        killed = sql("SELECT 1 FROM ledger_event e JOIN guild_member m ON m.guid = e.actor_guid "
                     f"AND m.guildid = {gid} WHERE e.event_type = 'death' AND e.subject_guid = {pg} "
                     f"AND e.ts > FROM_UNIXTIME({int(joined)}) LIMIT 1")
        kind, new_rank, failing = ladder_step(rank, s, officers.get(gid, 0), failing_since.get((gid, pg), 0.0), now,
                                              s["leader"] <= CAST_OUT_REGARD or bool(killed))
        rows.append((gid, pg, rank, failing))
        if kind and (gid, pg) not in pending:
            actions.append(dict(guild=gid, player=pg, name=name, kind=kind, rank=new_rank, prefer=0,
                                near=1 if kind == "promote" else 0))
    stmts = ["START TRANSACTION;", "DELETE FROM company_rank;"]
    for part in chunks(rows, 300):
        stmts.append("INSERT INTO company_rank (guildid, player_guid, seen_rank, failing_since) VALUES " + ",".join(
            f"({g},{p},{k},{f'FROM_UNIXTIME({int(f)})' if f else 'NULL'})" for g, p, k, f in part) + ";")
    stmts.append("COMMIT;")
    sql("\n".join(stmts), fetch=False)
    return actions


def company_invites():
    """Every cycle: stale actions expire, invites no longer offered are withdrawn, a raise that waited long enough goes
    by word, and each offered candidacy without a recent invite gets one. Returns the new invites."""
    expire = " OR ".join(f"(action = '{k}' AND created_at < NOW() - INTERVAL {h} HOUR)" for k, h in ACTION_EXPIRE_HOURS.items())
    sql(f"UPDATE company_action SET done_at = NOW(), result = 'expired' WHERE done_at IS NULL AND ({expire}); "
        # Only a real player's invite hangs on candidacy; a bot recruit's (P6) simply expires.
        "UPDATE company_action a JOIN characters pc ON pc.guid = a.player_guid "
        "JOIN person_kind ppk ON ppk.guid = pc.guid AND ppk.kind IN ('main', 'player') "
        "LEFT JOIN company_candidacy k ON k.guildid = a.guildid AND k.player_guid = a.player_guid "
        "AND k.stage = 'offered' SET a.done_at = NOW(), a.result = 'withdrawn' "
        "WHERE a.done_at IS NULL AND a.action = 'invite' AND k.guildid IS NULL; "
        "UPDATE company_action SET near_only = 0 WHERE done_at IS NULL AND action = 'promote' AND near_only = 1 "
        f"AND created_at < NOW() - INTERVAL {PROMOTE_NEAR_HOURS} HOUR;", fetch=False)
    rows = sql("SELECT k.guildid, k.player_guid, COALESCE(k.sponsor_guid, 0), p.name FROM company_candidacy k "
               "JOIN characters p ON p.guid = k.player_guid WHERE k.stage = 'offered' AND NOT EXISTS "
               "(SELECT 1 FROM company_action a WHERE a.guildid = k.guildid AND a.player_guid = k.player_guid "
               f"AND a.action = 'invite' AND (a.done_at IS NULL OR a.created_at > NOW() - INTERVAL {INVITE_RETRY_HOURS} HOUR "
               f"OR (a.result = 'declined' AND a.done_at > NOW() - INTERVAL {DECLINE_RETRY_HOURS} HOUR)))")
    invites = [dict(guild=int(r[0]), player=int(r[1]), prefer=int(r[2]), name="\t".join(r[3:]), kind="invite", rank=None,
                    near=1) for r in rows]
    # Plan 18 P8f: a bot that has left its company to move is asked in by the officer who wanted it, once it is free.
    for r in sql("SELECT ch.id, ch.bot_guid, ch.to_guild, COALESCE(ch.officer_guid, 0), c.name FROM company_churn ch "
                 "JOIN characters c ON c.guid = ch.bot_guid LEFT JOIN guild_member m ON m.guid = ch.bot_guid "
                 "WHERE ch.kind = 'move' AND ch.invited = 0 AND m.guid IS NULL AND ch.ts > NOW() - INTERVAL 1 DAY"):
        invites.append(dict(guild=int(r[2]), player=int(r[1]), prefer=int(r[3]), name="\t".join(r[4:]), kind="invite",
                            rank=None, near=0))
        sql(f"UPDATE company_churn SET invited = 1 WHERE id = {int(r[0])}", fetch=False)
    return invites


def create_actions(actions, names, meta):
    """Gives each action the words its officer whispers (a writing lane, or a plain line when none answers) and stores
    it. Returns how many were stored."""
    if not actions:
        return 0
    rank_names = {(int(r[0]), int(r[1])): "\t".join(r[2:]) for r in sql("SELECT guildid, rid, rname FROM guild_rank")}
    charters = {int(r[0]): "\t".join(r[1:]) for r in sql(
        "SELECT guildid, REPLACE(REPLACE(charter, CHAR(10), ' '), CHAR(9), ' ') FROM lore_guild")}
    # Who the player is, so the officer does not guess ("brother") and speaks to a person of their people; and who
    # speaks, for the rows a bot says in its own voice (plan 18 P6).
    guids = {a["player"] for a in actions} | {a["prefer"] for a in actions if a["kind"] in OWN_VOICE and a["prefer"]}
    who = {int(r[0]): (RACES.get(int(r[1]), "wanderer").lower(), "woman" if r[2] == "1" else "man", "\t".join(r[3:]))
           for r in sql(f"SELECT guid, race, gender, name FROM characters WHERE guid IN ({','.join(map(str, guids))})")}
    article = lambda race: "an" if race[0] in "aeiou" else "a"
    for a in actions:
        if a.get("quiet"):   # plan 18 P8e/f: a bot leaving a bot company goes without a word
            a["words"] = ""
            continue
        company = names.get(a["guild"], "the company")
        rank = rank_names.get((a["guild"], a["rank"]), "a new place") if a["rank"] is not None else ""
        race, gender, _ = who.get(a["player"], ("wanderer", "person", ""))
        extra = dict(player=a["name"], rank=rank, company=company, why=a.get("why", ""), asked=a.get("asked", ""))
        situation = (f"{a['name']} is {article(race)} {race} {gender}. " + ACTION_SITUATION[a["kind"]].format(**extra)
                     + (" Name that standing in what you say." if a["kind"] in ("promote", "demote") else ""))
        a["words"] = ACTION_FALLBACK[a["kind"]].format(**dict(extra, asked=a.get("refuse", "")))[:255]
        if a["kind"] in OWN_VOICE:
            s_race, s_gender, s_name = who.get(a["prefer"], ("wanderer", "person", "someone"))
            a["prompt"] = ACTION_OWN_PROMPT.format(speaker=s_name, speaker_desc=f"{article(s_race)} {s_race} {s_gender}",
                                                   player=a["name"], situation=situation)
        else:
            a["prompt"] = ACTION_PROMPT.format(company=company, charter=charters.get(a["guild"], ""), player=a["name"],
                                               situation=situation)

    pool = fleet.Pool(fleet.prefer_lanes("evo-quality,z13-qwen35", "evo-quality=1,z13-qwen35=1"),
                      log=lambda m: log(m) if "FAILED" in m else None)

    def job(a):
        def run(lane, attempt):
            text = re.sub(r"\s+", " ", lane.chat([{"role": "user", "content": a["prompt"]}], 90, temperature=0.8))
            text = text.strip().strip('"').translate(ASCII_PUNCT)
            if not 3 <= len(text.split()) <= 35 or re.search(r"\d", text):
                raise ValueError(f"unusable line: {text[:60]!r}")
            bad = meta.search(text)
            if bad:
                raise ValueError(f"out-of-world term: {bad.group(0)!r}")
            a["words"] = text[:255]
        return run

    for a in actions:
        if not a.get("quiet"):
            pool.submit("character", 1, job(a), f"{a['kind']} words for {a['name']}")
    pool.run()
    sql("INSERT INTO company_action (guildid, player_guid, action, near_only, prefer_guid, words) VALUES " + ",".join(
        f"({a['guild']},{a['player']},{q(a['kind'])},{a['near']},{a['prefer'] or 'NULL'},{q(a['words'])})"
        for a in actions), fetch=False)
    for a in actions:
        log(f"company action: {names.get(a['guild'], 'a company')} will {a['kind']} {a['name'] or a['prefer']}: {a['words']}")
    return len(actions)


# ---------------------------------------------------------------------------
# 8b. your company over time (plan 18 P6): in a company a real player leads, members who long think badly of the
# leader may leave, and its officers ask in unguilded bots they are fond of. Both hourly; both are company actions.

LIFE_SECONDS = 3600
LOYALTY_LINE = -30.0           # a member whose regard for the leader is at or below this ...
LOYALTY_HOURS = 48             # ... for this long ...
LOYALTY_LEAVE_CHANCE = 0.5     # ... leaves at this chance each hourly check (0 = never)
RECRUIT_CHANCE = 0.5           # each hourly check, a company may ask one recruit in (0 = never) ...
RECRUIT_EVERY_HOURS = 24       # ... and asks no more often than this
RECRUIT_OFFICER_REGARD = 40.0  # an officer (a bot at rank officer or above) this fond of an unguilded bot asks it
RECRUIT_LEVEL_GAP = 5          # the bots' own security refuses an invite from further apart than this
COMPANY_SIZE_MAX = 50        # operator 2026-09-16: the seeded 20 all sat at exactly 15, so P8d never had a place
                             # free. Safe to raise: the automatic filling is off and the daily caps hold the pace.
# The recruit's side, as mod-playerbots' regard gate judges an invite (AiPlayerbot.CompanyJoinRegard and friends).
JOIN_REGARD, JOIN_FRIEND_REGARD, FRIEND_REGARD, ENEMY_REGARD = 30.0, 15.0, 40.0, -40.0


def companies_led_by_players():
    """{guildid: (leader guid, leader name)} for every company a real player leads."""
    return {int(r[0]): (int(r[1]), "\t".join(r[2:])) for r in sql(
        "SELECT g.guildid, g.leaderguid, c.name FROM guild g JOIN characters c ON c.guid = g.leaderguid "
        "JOIN person_kind pk ON pk.guid = c.guid WHERE pk.kind IN ('main', 'player')")}


def loyalty_step(score, sour_since, now, roll):
    """One member's hourly check. Returns (leaves, sour_since); sour_since 0 when the member is content enough."""
    if score > LOYALTY_LINE:
        return False, 0.0
    sour_since = sour_since or float(now)
    return now - sour_since >= LOYALTY_HOURS * 3600 and roll < LOYALTY_LEAVE_CHANCE, sour_since


def recruit_would_join(back, friends, enemies):
    """Whether a recruit takes an invite, by mod-playerbots' JudgeInvite measures (plan 18 P2): back is its regard for
    the officer asking, friends and enemies the members it likes or hates."""
    if back <= ENEMY_REGARD or enemies:
        return False
    return back >= JOIN_REGARD or (friends > 0 and back >= JOIN_FRIEND_REGARD)


def loyalty(led, now):
    import random
    if not led:
        return []
    sour = {(int(r[0]), int(r[1])): float(r[2]) for r in sql(
        "SELECT guildid, bot_guid, UNIX_TIMESTAMP(sour_since) FROM company_loyalty")}
    pending = {(int(r[0]), int(r[1])) for r in sql(
        "SELECT guildid, prefer_guid FROM company_action WHERE done_at IS NULL AND action = 'leave'")}
    rows = sql("SELECT m.guildid, m.guid, COALESCE(r.score, 0), COALESCE(r.last_reason, '') FROM guild_member m "
               "JOIN guild g ON g.guildid = m.guildid JOIN characters c ON c.guid = m.guid "
               "JOIN person_kind pk ON pk.guid = c.guid AND pk.kind IN ('bot', 'alt') "
               "LEFT JOIN regard r ON r.bot_guid = m.guid AND r.other_guid = g.leaderguid "
               f"WHERE m.guildid IN ({','.join(map(str, led))})")
    actions, keep = [], []
    for r in rows:
        gid, bot, score, reason = int(r[0]), int(r[1]), float(r[2]), "\t".join(r[3:])
        leaves, since = loyalty_step(score, sour.get((gid, bot), 0.0), now, random.random())
        if since:
            keep.append((gid, bot, since))
        if leaves and (gid, bot) not in pending:
            leader, leader_name = led[gid]
            actions.append(dict(guild=gid, player=leader, name=leader_name, kind="leave", rank=None, prefer=bot, near=0,
                                why=f", since {reason}" if reason else ""))
    # Only these companies' rows: bot_society keeps the bot companies' own (plan 18 P8e).
    stmts = ["START TRANSACTION;", f"DELETE FROM company_loyalty WHERE guildid IN ({','.join(map(str, led))});"]
    for part in chunks(keep, 300):
        stmts.append("INSERT INTO company_loyalty (guildid, bot_guid, sour_since) VALUES "
                     + ",".join(f"({g},{b},FROM_UNIXTIME({int(s)}))" for g, b, s in part) + ";")
    stmts.append("COMMIT;")
    sql("\n".join(stmts), fetch=False)
    return actions


def recruit_candidates(company_ids, officer_regard, min_familiarity=0, busy=()):
    """[(company, officer, recruit, recruit's regard for the officer, recruit name, friends inside, enemies inside)]:
    online unguilded bots an online officer (a bot at officer rank or above) of those companies holds at officer_regard
    or more and knows min_familiarity moments, same faction, within RECRUIT_LEVEL_GAP levels; fondest first."""
    if not company_ids:
        return []
    ids = ",".join(map(str, company_ids))
    horde = ",".join(map(str, sorted(HORDE)))
    pairs = sql("SELECT m.guildid, r.bot_guid, r.other_guid, COALESCE(back.score, 0), rc.name FROM regard r "
                f"JOIN guild_member m ON m.guid = r.bot_guid AND m.guildid IN ({ids}) AND m.`rank` <= 1 "
                "JOIN characters oc ON oc.guid = r.bot_guid AND oc.online = 1 "
                "JOIN characters rc ON rc.guid = r.other_guid AND rc.online = 1 "
                "JOIN person_kind rpk ON rpk.guid = rc.guid AND rpk.kind IN ('bot', 'alt') "
                "LEFT JOIN guild_member rm ON rm.guid = r.other_guid "
                "LEFT JOIN regard back ON back.bot_guid = r.other_guid AND back.other_guid = r.bot_guid "
                f"WHERE r.score >= {officer_regard} AND r.familiarity >= {min_familiarity} AND rm.guid IS NULL "
                f"AND ABS(CAST(rc.level AS SIGNED) - CAST(oc.level AS SIGNED)) <= {RECRUIT_LEVEL_GAP} "
                f"AND (rc.race IN ({horde})) = (oc.race IN ({horde})) ORDER BY r.score DESC")
    recruits = sorted({int(p[2]) for p in pairs} - set(busy))
    if not recruits:
        return []
    inside = {(int(r[0]), int(r[1])): (int(r[2]), int(r[3])) for r in sql(
        f"SELECT r.bot_guid, m.guildid, CAST(SUM(r.score >= {FRIEND_REGARD}) AS UNSIGNED), "
        f"CAST(SUM(r.score <= {ENEMY_REGARD}) AS UNSIGNED) FROM regard r JOIN guild_member m ON m.guid = r.other_guid "
        f"AND m.guildid IN ({ids}) WHERE r.bot_guid IN ({','.join(map(str, recruits))}) GROUP BY r.bot_guid, m.guildid")}
    out = []
    for gid, officer, recruit, back, name in ((int(p[0]), int(p[1]), int(p[2]), float(p[3]), "\t".join(p[4:])) for p in pairs):
        if recruit in busy:
            continue
        friends, enemies = inside.get((recruit, gid), (0, 0))
        out.append((gid, officer, recruit, back, name, friends, enemies))
    return out


def recruiting(led):
    import random
    if not led or RECRUIT_CHANCE <= 0:
        return []
    ids = ",".join(map(str, led))
    size = {int(r[0]): int(r[1]) for r in sql(f"SELECT guildid, COUNT(*) FROM guild_member WHERE guildid IN ({ids}) GROUP BY guildid")}
    lately = {int(r[0]) for r in sql(
        "SELECT DISTINCT a.guildid FROM company_action a JOIN characters c ON c.guid = a.player_guid "
        "JOIN person_kind cpk ON cpk.guid = c.guid AND cpk.kind IN ('bot', 'alt') "
        f"WHERE a.action = 'invite' AND a.guildid IN ({ids}) AND a.created_at > NOW() - INTERVAL {RECRUIT_EVERY_HOURS} HOUR")}
    due = [g for g in led if g not in lately and size.get(g, 0) < COMPANY_SIZE_MAX and random.random() < RECRUIT_CHANCE]
    actions, asked, taken = [], set(), set()
    for gid, officer, recruit, back, name, friends, enemies in recruit_candidates(due, RECRUIT_OFFICER_REGARD):
        if gid in asked or recruit in taken or not recruit_would_join(back, friends, enemies):
            continue
        actions.append(dict(guild=gid, player=recruit, name=name, kind="invite", rank=None, prefer=officer, near=0))
        asked.add(gid)
        taken.add(recruit)
    return actions


def company_life():
    """Hourly: loyalty leavings and officers' recruits for every company a real player leads (P6), and the bot
    companies' recruiting, loyalty and moves (P8d-f). Returns new actions."""
    now = int(time.time())
    if now - cursor("life") < LIFE_SECONDS:
        return []
    sql(cursor_sql("life", now), fetch=False)
    led = companies_led_by_players()
    return loyalty(led, now) + recruiting(led) + bot_society(now)


# ---------------------------------------------------------------------------
# 8c. bot society in motion (plan 18 P8d, P8e, P8f): companies bots lead take in unguilded bots their officers know,
# lose members who have long soured on them, and win members from one another. Operator decisions: S3 twice the slow
# pace, S4 never across factions, S5 floor 10 and the last officer kept. Every decision is a company action and a
# company_churn row, which the daily caps count.

BOT_RECRUITS_PER_COMPANY_DAY = 2
BOT_RECRUIT_CHANCE = 0.5        # each hourly check, per company with a place free
BOT_RECRUIT_REGARD = 25.0       # the leader or an officer this fond of an unguilded bot ...
BOT_RECRUIT_FAMILIARITY = 4     # ... and this familiar with it asks it in
BOT_LOYALTY_LINE = -30.0        # attachment at or below this ...
BOT_LOYALTY_HOURS = 72          # ... for this long, and a member leaves its company (P8e) ...
BOT_LOYALTY_PER_DAY = 2         # ... at most this many a day, world-wide
MOVE_ATTACHMENT = 0.0           # a member whose attachment is below this ...
MOVE_REGARD = 50.0              # ... who holds the leader or an officer of another company this high ...
MOVE_OFFICER_REGARD = 25.0      # ... who holds it at least this high in turn, may be asked over (P8f) ...
MOVE_CHANCE = 0.5               # ... at this chance each hourly check ...
MOVES_PER_DAY = 4               # ... at most this many a day, world-wide
COMPANY_FLOOR = 10              # operator S5; mod-playerbots' AiPlayerbot.CompanyFloor holds the same line


def bot_companies():
    """{guildid: dict(leader, size, officers, horde)} for every company a bot leads."""
    horde = ",".join(map(str, sorted(HORDE)))
    return {int(r[0]): dict(leader=int(r[1]), size=int(r[2]), officers=int(float(r[3] or 0)), horde=2 * float(r[4]) > int(r[2]))
            for r in sql("SELECT g.guildid, g.leaderguid, COUNT(m.guid), SUM(m.`rank` = 1), SUM(c.race IN ({})) FROM guild g "
                         "JOIN characters lc ON lc.guid = g.leaderguid "
                         "JOIN person_kind lpk ON lpk.guid = lc.guid AND lpk.kind IN ('bot', 'alt') "
                         "JOIN guild_member m ON m.guildid = g.guildid JOIN characters c ON c.guid = m.guid "
                         "GROUP BY g.guildid, g.leaderguid".format(horde))}


def attachments():
    """{(guildid, bot): mean regard for the members of its own company}."""
    return {(int(r[0]), int(r[1])): float(r[2]) for r in sql(
        "SELECT mb.guildid, r.bot_guid, AVG(r.score) FROM regard r JOIN guild_member mb ON mb.guid = r.bot_guid "
        "JOIN guild_member mo ON mo.guid = r.other_guid AND mo.guildid = mb.guildid GROUP BY mb.guildid, r.bot_guid")}


def spareable(company, rank, bot):
    """A bot company can let this member go: never its leader, never its last officer, never down to the floor."""
    return (bot != company["leader"] and company["size"] > COMPANY_FLOOR
            and not (rank == RANK_OFFICER and company["officers"] <= 1))


def bot_recruit_would_join(back, friends, enemies):
    """A bot asked into a bot company by someone it knows: nobody it hates inside, and some warmth toward the one asking
    or a friend already there. Looser than for a real player's invite: bots have only begun to know each other."""
    if back <= ENEMY_REGARD or enemies:
        return False
    return back >= JOIN_FRIEND_REGARD or friends > 0


def bot_society(now):
    import random
    companies = bot_companies()
    if not companies:
        return []
    ids = ",".join(map(str, companies))
    ranks = {int(r[0]): int(r[1]) for r in sql("SELECT guid, `rank` FROM guild_member")}
    busy = {int(r[0]) for r in sql(
        "SELECT prefer_guid FROM company_action WHERE done_at IS NULL AND prefer_guid IS NOT NULL "
        "UNION SELECT player_guid FROM company_action WHERE done_at IS NULL "
        "UNION SELECT bot_guid FROM company_churn WHERE kind = 'move' AND invited = 0 AND ts > NOW() - INTERVAL 1 DAY")}
    churn = {r[0]: int(r[1]) for r in sql("SELECT kind, COUNT(*) FROM company_churn WHERE ts > NOW() - INTERVAL 24 HOUR GROUP BY kind")}
    attach = attachments()
    actions, rows = [], []   # rows: (kind, bot, from company, to company, officer) for company_churn

    def let_go(gid, bot):
        company = companies[gid]
        company["size"] -= 1
        if ranks.get(bot) == RANK_OFFICER:
            company["officers"] -= 1
        busy.add(bot)

    # P8e: a member long soured on its company leaves it, quietly.
    sour = {(int(r[0]), int(r[1])): float(r[2]) for r in sql(
        f"SELECT guildid, bot_guid, UNIX_TIMESTAMP(sour_since) FROM company_loyalty WHERE guildid IN ({ids})")}
    keep = []
    for (gid, bot), att in sorted(attach.items(), key=lambda kv: kv[1]):
        if gid not in companies or att > BOT_LOYALTY_LINE or bot == companies[gid]["leader"]:
            continue
        since = sour.get((gid, bot)) or float(now)
        keep.append((gid, bot, since))
        if (now - since >= BOT_LOYALTY_HOURS * 3600 and churn.get("loyalty", 0) < BOT_LOYALTY_PER_DAY and bot not in busy
                and spareable(companies[gid], ranks.get(bot, RANK_INITIATE), bot)):
            actions.append(dict(guild=gid, player=0, name="", kind="leave", rank=None, prefer=bot, near=0, quiet=True))
            rows.append(("loyalty", bot, gid, 0, 0))
            churn["loyalty"] = churn.get("loyalty", 0) + 1
            let_go(gid, bot)
    stmts = ["START TRANSACTION;", f"DELETE FROM company_loyalty WHERE guildid IN ({ids});"]
    for part in chunks(keep, 300):
        stmts.append("INSERT INTO company_loyalty (guildid, bot_guid, sour_since) VALUES "
                     + ",".join(f"({g},{b},FROM_UNIXTIME({int(s)}))" for g, b, s in part) + ";")
    stmts.append("COMMIT;")
    sql("\n".join(stmts), fetch=False)

    # P8f: a member cool toward its own people and warm toward another company's officer is asked over. It leaves now;
    # company_invites() has the officer ask it in once it is free.
    home = {bot: gid for gid, bot in attach}
    movers = [bot for (gid, bot), att in attach.items() if gid in companies and att < MOVE_ATTACHMENT
              and bot not in busy and spareable(companies[gid], ranks.get(bot, RANK_INITIATE), bot)]
    if movers and churn.get("move", 0) < MOVES_PER_DAY:
        for r in sql("SELECT r.bot_guid, r.other_guid, mo.guildid FROM regard r "
                     "JOIN guild_member mo ON mo.guid = r.other_guid AND mo.`rank` <= 1 "
                     "JOIN regard back ON back.bot_guid = r.other_guid AND back.other_guid = r.bot_guid "
                     "JOIN characters oc ON oc.guid = r.other_guid AND oc.online = 1 "
                     "JOIN characters bc ON bc.guid = r.bot_guid AND bc.online = 1 "
                     f"WHERE r.bot_guid IN ({','.join(map(str, movers))}) AND r.score >= {MOVE_REGARD} "
                     f"AND back.score >= {MOVE_OFFICER_REGARD} "
                     f"AND ABS(CAST(oc.level AS SIGNED) - CAST(bc.level AS SIGNED)) <= {RECRUIT_LEVEL_GAP} ORDER BY r.score DESC"):
            bot, officer, to = int(r[0]), int(r[1]), int(r[2])
            frm = home.get(bot)
            if churn.get("move", 0) >= MOVES_PER_DAY:
                break
            if bot in busy or frm not in companies or to not in companies or to == frm:
                continue
            src, dst = companies[frm], companies[to]
            if (src["horde"] != dst["horde"] or dst["size"] >= COMPANY_SIZE_MAX
                    or RELATIONS.get((min(frm, to), max(frm, to)), 0.0) <= PRESENCE_RIVAL_STANCE
                    or not spareable(src, ranks.get(bot, RANK_INITIATE), bot) or random.random() >= MOVE_CHANCE):
                continue
            actions.append(dict(guild=frm, player=0, name="", kind="leave", rank=None, prefer=bot, near=0, quiet=True))
            rows.append(("move", bot, frm, to, officer))
            churn["move"] = churn.get("move", 0) + 1
            let_go(frm, bot)
            dst["size"] += 1

    # P8d: a company with a place free asks in an unguilded bot its leader or an officer knows and likes.
    lately = {int(r[0]): int(r[1]) for r in sql(
        "SELECT to_guild, COUNT(*) FROM company_churn WHERE kind = 'recruit' AND ts > NOW() - INTERVAL 24 HOUR GROUP BY to_guild")}
    due = [g for g, c in companies.items() if c["size"] < COMPANY_SIZE_MAX
           and lately.get(g, 0) < BOT_RECRUITS_PER_COMPANY_DAY and random.random() < BOT_RECRUIT_CHANCE]
    asked, taken = set(), set()
    for gid, officer, recruit, back, name, friends, enemies in recruit_candidates(due, BOT_RECRUIT_REGARD,
                                                                                   BOT_RECRUIT_FAMILIARITY, busy):
        if gid in asked or recruit in taken or not bot_recruit_would_join(back, friends, enemies):
            continue
        actions.append(dict(guild=gid, player=recruit, name=name, kind="invite", rank=None, prefer=officer, near=0))
        rows.append(("recruit", recruit, 0, gid, officer))
        asked.add(gid)
        taken.add(recruit)

    if rows:
        sql("INSERT INTO company_churn (kind, bot_guid, from_guild, to_guild, officer_guid) VALUES " + ",".join(
            f"({q(k)},{b},{f or 'NULL'},{t or 'NULL'},{o or 'NULL'})" for k, b, f, t, o in rows), fetch=False)
        log("bot society: " + ", ".join(f"{k} {b}" for k, b, *_ in rows))
    return actions


# ---------------------------------------------------------------------------
# 10. acquaintance from presence (plan 18 P8a): bots come to know each other by being in the same place. The
# dashboard's /bots snapshot (every online character's map, area and position, refreshed every 2 s) is read once a
# cycle; two bots close together share a small moment. No LLM, no game objects. Real players are left out: their
# ties come from what they actually do and say.

DASHBOARD_BOTS = "http://127.0.0.1:8787/bots"
PRESENCE_DISTANCE = 40.0         # yards, same map and area
PRESENCE_PAIR_SECONDS = 3600     # a pair shares at most one moment an hour ...
PRESENCE_PAIR_PER_DAY = 8        # ... and this many a day
PRESENCE_PER_BOT_HOUR = 3        # a bot takes at most this many new moments an hour, nearest first
PRESENCE_RIVAL_STANCE = -30.0
PRESENCE_DELTA = {"grouped": 1.0, "fought": 0.8, "company": 0.5, "faction": 0.3, "rivals": -0.5}
# A capital's crowds are strangers passing: there only grouping, fighting side by side and company mates count. The
# first live read gave 155 moments in one cycle, nearly all "crossed paths in Darnassus".
CAPITAL_ZONES = {1519, 1537, 1657, 1637, 1638, 1497}
_PRESENCE_PAIRS = {}   # (lower guid, higher guid) -> [last moment, day, moments that day]
_PRESENCE_BOTS = {}    # guid -> [hour, moments that hour]
_PRESENCE_LOADED = False


def load_presence_state():
    """Read the limiters back after a restart. Both tables or neither: _PRESENCE_PAIRS carries the per-pair
    hour and per-day caps, _PRESENCE_BOTS the per-bot three-an-hour cap, and persisting only the first
    leaks the hourly cap on every restart. Anything staler than a day is dropped on the way in, which is
    the same bound the end-of-cycle sweep already applies. A missing or unreadable file is not an error:
    the limiters simply start empty, which is exactly today's behaviour."""
    global _PRESENCE_LOADED
    _PRESENCE_LOADED = True
    try:
        with open(PRESENCE_STATE) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return
    now, hour = time.time(), int(time.time() // 3600)
    for key, val in (saved.get("pairs") or {}).items():
        try:
            a, b = (int(p) for p in key.split(","))
            last, day, count = float(val[0]), int(val[1]), int(val[2])
        except (ValueError, TypeError, IndexError):
            continue
        if now - last <= 86400:
            _PRESENCE_PAIRS[(a, b)] = [last, day, count]
    for guid, val in (saved.get("bots") or {}).items():
        try:
            slot_hour, count = int(val[0]), int(val[1])
        except (ValueError, TypeError, IndexError):
            continue
        if slot_hour == hour:   # last hour's counts have already expired
            _PRESENCE_BOTS[int(guid)] = [slot_hour, count]
    log(f"presence limiters restored: {len(_PRESENCE_PAIRS)} pairs, {len(_PRESENCE_BOTS)} bots this hour")


def save_presence_state():
    """Write the limiters at the end of a cycle. Deliberately not derived from regard.updated_at: that
    column also moves for chat and event moments, so warm-starting from it would over-suppress presence
    after any busy cycle -- trading a measurable burst for an unmeasurable deficit."""
    state = {"pairs": {f"{a},{b}": v for (a, b), v in _PRESENCE_PAIRS.items()},
             "bots": {str(g): v for g, v in _PRESENCE_BOTS.items()}}
    tmp = PRESENCE_STATE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, PRESENCE_STATE)   # never leave a half-written file for the next start to read
    except OSError as e:
        log(f"could not save presence limiters: {e}")


def presence_moment(a, b, pa, pb):
    """What two bots found close together share, from their snapshot entries (a, b) and people rows (pa, pb).
    Returns (delta, why) or None."""
    same_faction = (pa["race"] in HORDE) == (pb["race"] in HORDE)
    zone = int(a.get("zone") or 0)
    place = land_name(zone) if zone in LAND else (a.get("zone_name") or "the wilds")
    if a.get("group_leader") and a.get("group_leader") == b.get("group_leader"):
        return PRESENCE_DELTA["grouped"], f"rode together in {place}"
    if same_faction and a.get("combat") and b.get("combat"):
        return PRESENCE_DELTA["fought"], f"fought side by side in {place}"
    ga, gb = pa["guild"], pb["guild"]
    if zone in CAPITAL_ZONES and not (ga and ga == gb):
        return None
    if ga and gb and ga != gb and RELATIONS.get((min(ga, gb), max(ga, gb)), 0.0) <= PRESENCE_RIVAL_STANCE:
        return PRESENCE_DELTA["rivals"], f"crossed paths in {place}, though your companies are at odds"
    if ga and ga == gb:
        return PRESENCE_DELTA["company"], f"crossed paths with a fellow of your company in {place}"
    if same_faction:
        return PRESENCE_DELTA["faction"], f"crossed paths in {place}"
    return None


def presence(changes, snapshot=None):
    """Adds this cycle's moments to changes. snapshot is the /bots JSON (read from the dashboard when None). Returns the
    number of moments; 0 when the dashboard is away."""
    if snapshot is None:
        try:
            with urllib.request.urlopen(DASHBOARD_BOTS, timeout=5) as resp:
                snapshot = json.load(resp)
        except (OSError, ValueError):
            return 0
    if not _PRESENCE_LOADED:
        load_presence_state()
    now = time.time()
    day, hour = int(now // 86400), int(now // 3600)
    near = {}
    for p in snapshot.get("players", []):
        if p.get("bot") and not p.get("dead") and not p.get("flight"):
            near.setdefault((p.get("map"), p.get("area") or p.get("zone")), []).append(p)
    pairs = []
    for group in near.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                d2 = (a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2
                if d2 <= PRESENCE_DISTANCE ** 2:
                    pairs.append((d2, a, b))
    if not pairs:
        return 0
    pairs.sort(key=lambda t: t[0])
    cast = people({p["guid"] for _, a, b in pairs for p in (a, b)})

    def room(guid):
        slot = _PRESENCE_BOTS.get(guid)
        return not slot or slot[0] != hour or slot[1] < PRESENCE_PER_BOT_HOUR

    moments = 0
    for _, a, b in pairs:
        key = (min(a["guid"], b["guid"]), max(a["guid"], b["guid"]))
        state = _PRESENCE_PAIRS.get(key)
        if state and (now - state[0] < PRESENCE_PAIR_SECONDS or (state[1] == day and state[2] >= PRESENCE_PAIR_PER_DAY)):
            continue
        pa, pb = cast.get(a["guid"]), cast.get(b["guid"])
        if not pa or not pb or not room(a["guid"]) or not room(b["guid"]):
            continue
        moment = presence_moment(a, b, pa, pb)
        if not moment:
            continue
        delta, why = moment
        changes.add(a["guid"], b["guid"], delta, "presence", why)
        changes.add(b["guid"], a["guid"], delta, "presence", why)
        _PRESENCE_PAIRS[key] = [now, day, (state[2] + 1) if state and state[1] == day else 1]
        for guid in (a["guid"], b["guid"]):
            slot = _PRESENCE_BOTS.get(guid)
            _PRESENCE_BOTS[guid] = [hour, slot[1] + 1] if slot and slot[0] == hour else [hour, 1]
        moments += 1
    # Forget pairs not seen for a day, so the service's memory stays small.
    for key in [k for k, v in _PRESENCE_PAIRS.items() if now - v[0] > 86400]:
        del _PRESENCE_PAIRS[key]
    save_presence_state()
    return moments


def fetch_positions():
    """The dashboard's /bots snapshot, or None when it is away."""
    try:
        with urllib.request.urlopen(DASHBOARD_BOTS, timeout=5) as resp:
            return json.load(resp)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 11. bot talk (plan 18 P8c): now and then two bots idling near each other fall to talking, while no real player is in
# the world to need the fleet. A writing lane writes the exchange from who they are and what they feel about each
# other; the judge reads it like any chat, and their feelings move. It is kept in bot_talk for the dashboard. Nothing is
# said in the game.

TALK_OFF = os.path.join(HERE, "NO_TALK")   # kill switch
TALK_DISTANCE = 15.0
TALK_IDLE = {0, 4, 7}          # the dashboard's rpg states: idle, visiting someone, resting
TALKS_PER_CYCLE = 1
TALKS_PER_HOUR = 12
TALK_PAIR_SECONDS = 86400      # the same two talk at most once a day
TALK_SHARE = 0.5               # a judged moment in a talk counts for this share of one in heard chat ...
TALK_CAP = 8.0                 # ... and one talk moves a feeling at most this far either way
TALK_PROMPT = """{setting}

Two people meet {place} and fall to talking, with no one else listening.

{a_block}

{b_block}
{news}
Write their conversation: {n} lines, taking turns, starting with {first}. Vary how long the lines run: most are a handful of words, and only a few reach twenty or more, when someone actually has something to say. Write each in the speaker's own voice and manner. They talk as people do: the day, their work or company, the land, what drives them, or each other. Let what each feels about the other show. Vary how the lines open, and keep the weather out of it: no line beginning "The wind here", "The earth here" or "The silence", and none ending by asking "don't you?" or "isn't it?". No narration, no numbers, nothing outside the world.
Reply with JSON only: {{"lines": [{{"speaker": "{first}", "text": "..."}}]}}"""
_TALK_TIMES = []   # when conversations were written this hour
_TALK_PAIRS = {}   # (lower guid, higher guid) -> when they last talked (or were tried)


def talk_pairs(snapshot, now):
    """[(a, b)] idle same-faction bots close together that have not talked today, nearest first (at most 60)."""
    near = {}
    for p in snapshot.get("players", []):
        if (p.get("bot") and not p.get("dead") and not p.get("flight") and not p.get("combat")
                and int(p.get("rpg") or 0) in TALK_IDLE):
            near.setdefault((p.get("map"), p.get("area") or p.get("zone")), []).append(p)
    pairs = []
    for group in near.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                d2 = (a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2
                key = (min(a["guid"], b["guid"]), max(a["guid"], b["guid"]))
                if (d2 <= TALK_DISTANCE ** 2 and now - _TALK_PAIRS.get(key, 0) >= TALK_PAIR_SECONDS
                        and (a.get("race") in HORDE) == (b.get("race") in HORDE)):
                    pairs.append((d2, a, b))
    pairs.sort(key=lambda t: t[0])
    return [(a, b) for _, a, b in pairs[:60]]


def talk_block(p, other, feeling, lore, company, gender, level, e):
    race, cls = RACES.get(p["race"], "wanderer"), CLASSES.get(p["cls"], "adventurer")
    personality, gist, drive = lore
    lines = [f"{p['name']}: {'a woman' if gender else 'a man'}, {race} {cls}, {eras.standing(level, e)}. "
             + (f"Sworn to the company {company}." if company else "Sworn to no company.")]
    if personality or gist:
        lines.append(f"In {p['name']}'s own words: {personality} {gist}".strip())
    if drive:
        lines.append(f"What drives {p['name']}: {drive}")
    if feeling:
        lines.append(f"How {p['name']} feels about {other}: {words(feeling[0]).replace('you ', 'they ', 1).replace('them', other)}."
                     + (f" In {p['name']}'s thoughts: {feeling[1]}" if feeling[1] else ""))
    else:
        lines.append(f"{p['name']} does not know {other}.")
    return "\n".join(lines)


def write_talk(a, b, pa, pb, feel, e, meta, names):
    """The exchange as {lines: [(person, text)], model, place, zone, map}, or None when no lane gives a usable one."""
    import random
    ids = f"{pa['guid']},{pb['guid']}"
    about = {int(r[0]): (int(r[1]), int(r[2])) for r in sql(f"SELECT guid, gender, level FROM characters WHERE guid IN ({ids})")}
    lore = {int(r[0]): tuple(r[1:4]) for r in sql(
        "SELECT guid, REPLACE(COALESCE(personality, ''), CHAR(9), ' '), REPLACE(COALESCE(gist, ''), CHAR(9), ' '), "
        f"REPLACE(COALESCE(motivation_short, ''), CHAR(9), ' ') FROM lore_character WHERE guid IN ({ids})")}
    zone = int(a.get("zone") or 0)
    place = "in " + (land_name(zone) if zone in LAND else (a.get("zone_name") or "the wilds"))
    news = sql(f"SELECT words FROM land_words WHERE zone_id = {zone}")
    first = random.choice([pa, pb])

    def block(p, other):
        gender, level = about.get(p["guid"], (0, 1))
        return talk_block(p, other["name"], feel.get((p["guid"], other["guid"])), lore.get(p["guid"], ("", "", "")),
                          names.get(p["guild"]), gender, level, e)

    prompt = TALK_PROMPT.format(setting=e["setting"], place=place, a_block=block(pa, pb), b_block=block(pb, pa),
                                news=f"\nWhat is said in these parts: {chr(9).join(news[0])}\n" if news else "",
                                n=random.randint(4, 6), first=first["name"])
    result = {}
    by_name = {pa["name"].lower(): pa, pb["name"].lower(): pb}

    def run(lane, attempt):
        out = lane.chat([{"role": "user", "content": prompt}], 600, temperature=0.9)
        m = re.search(r"\{.*\}", out, re.S)
        data = json.loads(m.group(0)) if m else {}
        lines = []
        for item in data.get("lines") or []:
            if not isinstance(item, dict):
                continue
            who = by_name.get(str(item.get("speaker", "")).strip().lower())
            text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip().strip('"').translate(ASCII_PUNCT)
            if not who or not text:
                continue
            if len(text.split()) > 40 or re.search(r"\d", text):
                raise ValueError(f"unusable line: {text[:50]!r}")
            bad = meta.search(text)
            if bad:
                raise ValueError(f"out-of-world term: {bad.group(0)!r}")
            lines.append((who, text))
        if len(lines) < 3 or len({w["guid"] for w, _ in lines}) < 2:
            raise ValueError(f"only {len(lines)} usable lines")
        result.update(lines=lines, model=(lane.name + ":" + lane.model)[:64])

    pool = fleet.Pool(fleet.prefer_lanes("evo-quality,z13-qwen35", "evo-quality=1,z13-qwen35=1"),
                      log=lambda msg: log(msg) if "FAILED" in msg else None)
    pool.submit("character", 1, run, f"talk {pa['name']} and {pb['name']}")
    pool.run()
    return dict(result, place=place, zone=zone, map=int(a.get("map") or 0)) if result.get("lines") else None


def judge_talk(changes, talk, pa, pb, judge, meta):
    """The judge reads the exchange like any chat; its moments move both bots' feelings. Returns the moments kept."""
    text = "\n".join(f"{i}. {who['name']}: {line}" for i, (who, line) in enumerate(talk["lines"], 1))
    try:
        out = judge.chat([{"role": "user", "content": JUDGE_PROMPT.format(people=f"{pa['name']}, {pb['name']}", lines=text)}],
                         900, temperature=0.1, json_mode=True)
        m = re.search(r"\{.*\}", out, re.S)
        moments = json.loads(m.group(0)).get("moments", []) if m else []
    except (RuntimeError, ValueError):
        return []
    involved = {pa["name"].lower(): pa, pb["name"].lower(): pb}
    kept = []
    moved = {}   # (feeler, about) -> how far this talk has moved it so far
    def add(feeler, about, delta, why):
        # A talk no player heard counts for less than chat, and one talk never swings a feeling far: the first dry run
        # gave five moments worth -20 in a single exchange.
        room = TALK_CAP - abs(moved.get((feeler, about), 0.0))
        step = max(-room, min(room, delta * TALK_SHARE))
        if step:
            moved[(feeler, about)] = moved.get((feeler, about), 0.0) + step
            changes.add(feeler, about, step, "talk", why)
    for mo in moments if isinstance(moments, list) else []:
        if not isinstance(mo, dict):
            continue
        speaker, target = involved.get(str(mo.get("speaker", "")).lower()), involved.get(str(mo.get("target", "")).lower())
        try:
            target_feels = max(-10.0, min(10.0, float(mo.get("target_feels", 0))))
            speaker_feels = max(-10.0, min(10.0, float(mo.get("speaker_feels", 0))))
        except (TypeError, ValueError):
            continue
        if not speaker or not target or speaker["guid"] == target["guid"]:
            continue
        why, speaker_why, what = moment_words(mo, speaker, target, meta)
        add(target["guid"], speaker["guid"], target_feels, why)
        add(speaker["guid"], target["guid"], speaker_feels * SPEAKER_SHARE, speaker_why)
        changes.words.append((speaker, target, target_feels * TALK_SHARE, what, dict(zone=talk["zone"], map=talk["map"])))
        kept.append(dict(speaker=speaker["name"], target=target["name"], feels=target_feels, why=what))
    return kept


def bot_talk(changes, snapshot, judge, e, meta, names):
    """Writes, judges and stores at most TALKS_PER_CYCLE conversations. Returns how many."""
    now = time.time()
    if not snapshot or os.path.exists(TALK_OFF) or int((snapshot.get("counts") or {}).get("real") or 0) > 0:
        return 0
    _TALK_TIMES[:] = [t for t in _TALK_TIMES if now - t < 3600]
    pairs = talk_pairs(snapshot, now)
    if not pairs or len(_TALK_TIMES) >= TALKS_PER_HOUR:
        return 0
    cast = people({p["guid"] for pair in pairs for p in pair})
    ids = ",".join(map(str, sorted({p["guid"] for pair in pairs for p in pair})))
    feel = {(int(r[0]), int(r[1])): (float(r[2]), "\t".join(r[3:])) for r in sql(
        "SELECT bot_guid, other_guid, score, REPLACE(REPLACE(COALESCE(description, ''), CHAR(10), ' '), CHAR(9), ' ') "
        f"FROM regard WHERE bot_guid IN ({ids}) AND other_guid IN ({ids})")}
    # People who know each other first; the sort is stable, so nearest first within each.
    pairs.sort(key=lambda ab: -(((ab[0]["guid"], ab[1]["guid"]) in feel) + ((ab[1]["guid"], ab[0]["guid"]) in feel)))
    done = 0
    for a, b in pairs:
        if done >= TALKS_PER_CYCLE:
            break
        pa, pb = cast.get(a["guid"]), cast.get(b["guid"])
        if not pa or not pb or not pa["bot"] or not pb["bot"]:
            continue
        _TALK_PAIRS[(min(pa["guid"], pb["guid"]), max(pa["guid"], pb["guid"]))] = now   # tried, whatever comes of it
        talk = write_talk(a, b, pa, pb, feel, e, meta, names)
        if not talk:
            continue
        _TALK_TIMES.append(now)
        done += 1
        kept = judge_talk(changes, talk, pa, pb, judge, meta)
        sql("INSERT INTO bot_talk (map_id, zone_id, place, a_guid, b_guid, exchange, moments, model) VALUES "
            f"({talk['map']}, {talk['zone']}, {q(talk['place'][:64])}, {pa['guid']}, {pb['guid']}, "
            f"{q(json.dumps([[w['name'], t] for w, t in talk['lines']]))}, {q(json.dumps(kept))}, {q(talk['model'])})",
            fetch=False)
        log(f"talk {talk['place']}: {pa['name']} and {pb['name']}, {len(talk['lines'])} lines, {len(kept)} moments")
    return done


# ---------------------------------------------------------------------------
# 9. founding (plan 18 P5): a company raised by a real player's charter gets its story from founding.py, started in
# the background so a slow lane never holds up the cycle.

FOUNDING_OFF = os.path.join(HERE, "NO_FOUNDING")   # kill switch: no runs are started
FOUNDING_LOCK = os.path.join(HERE, "founding.lock")
FOUNDING_LOG = os.path.join(HERE, "founding.log")
FOUNDING_RETRY_SECONDS = 1800
FOUNDING_ATTEMPTS = 3


def founding_due():
    """Ids of companies led by a real player that have no story and no founding yet, or whose unfinished founding may
    be tried again. A seeded company has lore and no founding row, so it is never due."""
    rows = sql("SELECT g.guildid, COALESCE(f.state, ''), COALESCE(f.attempts, 0), COALESCE(UNIX_TIMESTAMP(f.last_attempt), 0), "
               "EXISTS(SELECT 1 FROM lore_guild l WHERE l.guildid = g.guildid) FROM guild g "
               "JOIN characters c ON c.guid = g.leaderguid JOIN person_kind pk ON pk.guid = c.guid "
               "LEFT JOIN company_founding f ON f.guildid = g.guildid AND f.founded_at = g.createdate "
               "WHERE pk.kind IN ('main', 'player')")
    now = time.time()
    return [int(gid) for gid, state, attempts, last, lore in rows
            if (state == "" and lore == "0")
            or (state not in ("", "done") and int(attempts) < FOUNDING_ATTEMPTS and now - float(last) >= FOUNDING_RETRY_SECONDS)]


def start_founding(era):
    """Starts `founding.py once` in the background when a company is due and no run holds the lock. Returns the ids."""
    import fcntl
    if os.path.exists(FOUNDING_OFF):
        return []
    todo = founding_due()
    if not todo:
        return []
    with open(FOUNDING_LOCK, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return []   # a run is still going
        fcntl.flock(lock, fcntl.LOCK_UN)
    with open(FOUNDING_LOG, "a") as out:
        subprocess.Popen([sys.executable, os.path.join(HERE, "founding.py"), "once", "--era", era],
                         stdout=out, stderr=subprocess.STDOUT, cwd=HERE, start_new_session=True)
    log(f"founding started for companies {todo}")
    return todo


# ---------------------------------------------------------------------------
# 2. chat

JUDGE_PROMPT = """These words were spoken aloud in Azeroth, in this order. The people involved: {people}.

{lines}

For each moment where a speaker talks to or about one of those people in a way that changes how they feel about each other (an insult, praise, thanks, a threat, mockery, help offered, blame, an apology, flirting), give:
- "target_feels": how the person spoken to or about now feels toward the speaker, from -10 (deeply hurt or enraged) to 10 (touched or grateful);
- "speaker_feels": how the speaker feels toward that person, from -10 to 10;
- "why": how the person spoken to or about remembers it, under 12 words, speaking to them as "you" and of the speaker as "they" (for example "they mocked your candlelight");
- "speaker_why": how the speaker remembers it, under 12 words, "you" being the speaker and "they" the other (for example "you mocked their candlelight");
- "what": what happened as a bystander would tell it, under 12 words, with both their names (for example "Hesk mocked Ilda's candlelight", using the real names).
No numbers, nothing outside the world. Leave out small talk that changes nothing. Use the names exactly as listed.
Reply with JSON only: {{"moments": [{{"line": 1, "speaker": "Name", "target": "Name", "target_feels": 0, "speaker_feels": 0, "why": "...", "speaker_why": "...", "what": "..."}}]}}"""


def moment_words(mo, speaker, target, meta):
    """The judge's three tellings of a moment: how the person spoken to remembers it and how the speaker does (each
    goes into that one's own regard reason, so prompts read "since they mocked your candlelight"), and a bystander's,
    with names, for company incidents and the dashboard."""
    def telling(key, fallback):
        text = re.sub(r"\s+", " ", str(mo.get(key) or "")).strip().strip('"')
        # A memory that slips into "I" ("you walk to emptiness while I walk to her voice") is quoting, not remembering.
        return fallback if not text or meta.search(text) or re.search(r"\bI\b|\b(?i:me|my)\b", text) else text
    return (telling("why", "words passed between you"), telling("speaker_why", "words passed between you"),
            telling("what", f"words passed between {speaker['name']} and {target['name']}"))


class JudgeUnreachable(Exception):
    pass


def process_chat(changes, judge, meta):
    """Returns (last_id, lines, conversations, moments). Raises JudgeUnreachable to leave the batch."""
    cur = cursor("chat")
    rows = sql("SELECT id, UNIX_TIMESTAMP(ts), speaker_guid, speaker_name, COALESCE(listener_guid, 0), chat_type, "
               "COALESCE(channel, ''), zone_id, map_id, text FROM ledger_chat "
               f"WHERE id > {cur} AND ts < NOW() - INTERVAL {CHAT_SETTLE_SECONDS} SECOND ORDER BY id LIMIT {CHAT_BATCH}")
    if not rows:
        return cur, 0, 0, 0
    lines = [dict(id=int(r[0]), ts=float(r[1]), speaker=int(r[2]), name=r[3], listener=int(r[4]), type=int(r[5]),
                  channel=r[6], zone=int(r[7]), map=int(r[8]), text="\t".join(r[9:])) for r in rows]
    last = lines[-1]["id"]

    cast = people({l["speaker"] for l in lines} | {l["listener"] for l in lines if l["type"] == 7})
    runs = {}
    for l in lines:
        kind = TYPE_CLASS.get(l["type"])
        if not kind:
            continue
        if kind in ("local", "group"):
            key = (kind, l["map"], l["zone"])
        elif kind == "guild":
            key = (kind, cast.get(l["speaker"], {}).get("guild", 0))
        elif kind == "whisper":
            key = (kind,) + tuple(sorted((l["speaker"], l["listener"])))
        else:
            key = (kind, l["channel"], l["map"])
        seq = runs.setdefault(key, [])
        if seq and l["ts"] - seq[-1][-1]["ts"] <= WINDOW_GAP_SECONDS:
            seq[-1].append(l)
        else:
            seq.append([l])
    conversations = [run[-WINDOW_MAX_LINES:] for seq in runs.values() for run in seq]

    named = people_by_name({n for c in conversations for l in c for n in NAME_RE.findall(l["text"])})
    jobs = []
    for c in conversations:
        involved = {}
        for l in c:
            if l["speaker"] in cast:
                involved[cast[l["speaker"]]["name"].lower()] = cast[l["speaker"]]
            if l["type"] == 7 and l["listener"] in cast:
                involved[cast[l["listener"]]["name"].lower()] = cast[l["listener"]]
            for n in NAME_RE.findall(l["text"]):
                if n.lower() in named:
                    involved[n.lower()] = named[n.lower()]
        if len(involved) >= 2 and any(p["bot"] for p in involved.values()):
            jobs.append((c, involved))

    def ask(job):
        c, involved = job
        text = []
        for i, l in enumerate(c, 1):
            who = cast.get(l["speaker"], {}).get("name", l["name"])
            to = f" (to {cast[l['listener']]['name']})" if l["type"] == 7 and l["listener"] in cast else ""
            text.append(f"{i}. {who}{to}: {l['text']}")
        prompt = JUDGE_PROMPT.format(people=", ".join(sorted(p["name"] for p in involved.values())),
                                     lines="\n".join(text))
        for _ in range(2):
            try:
                out = judge.chat([{"role": "user", "content": prompt}], 900, temperature=0.1, json_mode=True)
            except RuntimeError as e:
                raise JudgeUnreachable(str(e)) from None
            m = re.search(r"\{.*\}", out, re.S)
            try:
                moments = json.loads(m.group(0)).get("moments", []) if m else None
            except ValueError:
                moments = None
            if isinstance(moments, list):
                return c, involved, moments
        return c, involved, []   # the judge answered twice with nothing usable: count it as nothing happened

    moments = 0
    with cf.ThreadPoolExecutor(JUDGE_SLOTS) as pool:
        for c, involved, found in pool.map(ask, jobs):
            for mo in found:
                if not isinstance(mo, dict):
                    continue
                speaker = involved.get(str(mo.get("speaker", "")).lower())
                target = involved.get(str(mo.get("target", "")).lower())
                try:
                    line = c[max(1, min(len(c), int(mo.get("line", 1)))) - 1]
                    target_feels = max(-10.0, min(10.0, float(mo.get("target_feels", 0))))
                    speaker_feels = max(-10.0, min(10.0, float(mo.get("speaker_feels", 0))))
                except (TypeError, ValueError):
                    continue
                if not speaker or not target or speaker["guid"] == target["guid"]:
                    continue
                why, speaker_why, what = moment_words(mo, speaker, target, meta)
                changes.add(target["guid"], speaker["guid"], target_feels, "chat", why, line["id"])
                changes.add(speaker["guid"], target["guid"], speaker_feels * SPEAKER_SHARE, "chat", speaker_why, line["id"])
                changes.words.append((speaker, target, target_feels, what, line))
                moments += 1
    return last, len(lines), len(jobs), moments


# ---------------------------------------------------------------------------
# 3. decay, 4. words, 5. snapshot

FORGET_SCORE = 3.0         # a stranger felt this faintly about ...
FORGET_DAYS = 7            # ... and not met again for this long is forgotten (plan 18 P8a: presence makes many)


def decay():
    sql("UPDATE regard SET score = baseline + (score - baseline) * "
        f"POW({DECAY_PER_DAY}, TIMESTAMPDIFF(MINUTE, decayed_at, NOW()) / 1440.0), decayed_at = NOW() "
        "WHERE decayed_at < NOW() - INTERVAL 1 HOUR; "
        # No sentence, no company warmth, barely a feeling: a face in the crowd.
        f"DELETE FROM regard WHERE baseline = 0 AND ABS(score) < {FORGET_SCORE} AND (description IS NULL OR description = '') "
        f"AND updated_at < NOW() - INTERVAL {FORGET_DAYS} DAY;", fetch=False)


DESCRIBE_PROMPT = """You are writing one private thought for {name}, a {race} {cls} living in Azeroth.
How {name} feels about {other}: {words}.
What passed between them, most recent first:
{moments}

Write ONE sentence of at most 30 words, in the second person ("You ..."), saying how you feel about {other} and why. Plain words, no quotes. Nothing outside the world: no numbers, no game terms."""


def describe(meta):
    gap = "ABS(r.score - COALESCE(r.described_score, r.baseline))"
    rows = sql("SELECT r.bot_guid, r.other_guid, r.score, b.name, b.race, b.class, o.name FROM regard r "
               "JOIN characters b ON b.guid = r.bot_guid JOIN characters o ON o.guid = r.other_guid "
               f"WHERE r.familiarity >= 2 AND {gap} >= {DESCRIBE_AFTER} ORDER BY {gap} DESC LIMIT {DESCRIBE_PER_CYCLE}")
    if not rows:
        return 0
    # One slot on each fast lane: live chat shares these backends.
    pool = fleet.Pool(fleet.prefer_lanes("evo-quality,z13-qwen35", "evo-quality=1,z13-qwen35=1"),
                      log=lambda m: log(m) if "FAILED" in m else None)
    done = []

    def job(r):
        bot, other, score = int(r[0]), int(r[1]), float(r[2])
        name, race, cls, other_name = r[3], RACES.get(int(r[4]), "person"), CLASSES.get(int(r[5]), "wanderer"), r[6]
        reasons = [x[0] for x in sql(f"SELECT reason FROM regard_log WHERE bot_guid = {bot} AND other_guid = {other} "
                                     "ORDER BY id DESC LIMIT 6")]
        prompt = DESCRIBE_PROMPT.format(name=name, race=race, cls=cls, other=other_name, words=words(score),
                                        moments="\n".join(f"- {x}" for x in reasons) or "- little of note")

        def run(lane, attempt):
            text = re.sub(r"\s+", " ", lane.chat([{"role": "user", "content": prompt}], 120, temperature=0.7))
            text = text.strip().strip('"').translate(ASCII_PUNCT)
            if not text.lower().startswith("you") or len(text.split()) > 45:
                raise ValueError(f"unusable sentence: {text[:60]!r}")
            bad = meta.search(text)
            if bad:
                raise ValueError(f"out-of-world term: {bad.group(0)!r}")
            sql(f"UPDATE regard SET description = {q(text[:255])}, described_score = {score:.2f} "
                f"WHERE bot_guid = {bot} AND other_guid = {other}", fetch=False)
            done.append(1)
        return run

    for r in rows:
        pool.submit("character", 1, job(r), f"words {r[3]} about {r[6]}")
    pool.run()
    return len(done)


def write_json(path, obj):
    """Write a snapshot file whole: readers see the old one or the new one, never half of either."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)


_ties_written = {}   # guid -> digest of the file as last written; a cycle rewrites only what moved


def write_ties(everyone):
    """One file per person under TIES_DIR, holding every tie they are part of, in both directions.

    The dashboard fetches a person's file only when their Ties tab is opened. Nothing in the file is
    stamped with the time, so a person whose feelings did not move this cycle is not rewritten -- with
    ~1,300 people and a cycle every two minutes that matters more than the bytes do.
    """
    os.makedirs(TIES_DIR, exist_ok=True)
    for guid, p in everyone.items():
        body = json.dumps(dict(guid=guid, name=p["name"], bot=p["bot"], feels=p["feels"], felt_by=p["felt_by"]),
                          separators=(",", ":"))
        digest = hashlib.blake2s(body.encode(), digest_size=8).hexdigest()
        path = os.path.join(TIES_DIR, f"{guid}.json")
        if _ties_written.get(guid) == digest and os.path.exists(path):
            continue
        with open(path + ".tmp", "w") as f:
            f.write(body)
        os.replace(path + ".tmp", path)
        _ties_written[guid] = digest
    for name in os.listdir(TIES_DIR):   # their last tie decayed away, the character is gone, or a stray .tmp
        guid = name[:-5]
        if name.endswith(".json") and guid.isdigit() and int(guid) in everyone:
            continue
        os.remove(os.path.join(TIES_DIR, name))
        if guid.isdigit():
            _ties_written.pop(int(guid), None)


def write_archives(ties, generated):
    """Everything behind the Feelings panel's four lists, for its full-screen views.

    The panel shows the head of each: the latest 25 moments, the latest 15 conversations, ten warm ties
    and ten cold. These three files hold every row there is, so the operator can read down the lot and
    check the scoring. They are fetched only when a full-screen view is opened, never polled, and the
    rows are arrays with a name table beside them -- 97,000 ties as objects weigh 19 MB, as rows 2.

    Returns the totals, which go into regard.json so the panel can say how much it is not showing.
    """
    rows = sql("SELECT UNIX_TIMESTAMP(l.ts), l.bot_guid, b.name, l.other_guid, o.name, l.delta, l.score, "
               "l.source, l.reason FROM regard_log l JOIN characters b ON b.guid = l.bot_guid "
               "JOIN characters o ON o.guid = l.other_guid ORDER BY l.id DESC")
    moments = [[float(r[0]), int(r[1]), int(r[3]), round(float(r[5]), 1), round(float(r[6]), 1),
                r[7], "\t".join(r[8:])] for r in rows]
    names = {int(r[1]): r[2] for r in rows} | {int(r[3]): r[4] for r in rows}
    write_json(MOMENTS_SNAPSHOT, dict(generated=generated, count=len(moments), names=names, moments=moments))

    talks = [dict(ts=float(r[0]), place=r[1], a=r[2], b=r[3], exchange=json.loads(r[4]),
                  moments=json.loads("\t".join(r[5:])))
             for r in sql("SELECT UNIX_TIMESTAMP(t.ts), t.place, a.name, b.name, t.exchange, t.moments "
                          "FROM bot_talk t JOIN characters a ON a.guid = t.a_guid "
                          "JOIN characters b ON b.guid = t.b_guid ORDER BY t.id DESC")]
    write_json(TALKS_SNAPSHOT, dict(generated=generated, count=len(talks), talks=talks))

    # Warmest first, coldest last: one ranking read from either end. The seven phrases words() can
    # return are shipped once and pointed at, rather than spelled out 97,000 times.
    phrases, seen = [], {}
    ranked = []
    for t in sorted(ties, key=lambda t: -t["score"]):
        if t["words"] not in seen:
            seen[t["words"]] = len(phrases)
            phrases.append(t["words"])
        ranked.append([t["feeler"], t["about"], t["score"], t["familiarity"], seen[t["words"]]])
    names = {t["feeler"]: t["feeler_name"] for t in ties} | {t["about"]: t["about_name"] for t in ties}
    write_json(RANKED_SNAPSHOT, dict(generated=generated, count=len(ranked), names=names,
                                     phrases=phrases, ties=ranked))
    return dict(moments=len(moments), talks=len(talks), ties=len(ranked),
                warm=sum(1 for t in ranked if t[2] > 0), cold=sum(1 for t in ranked if t[2] < 0))


def snapshot():
    pairs = int(sql("SELECT COUNT(*) FROM regard")[0][0])
    moments_day = int(sql("SELECT COUNT(*) FROM regard_log WHERE ts > NOW() - INTERVAL 1 DAY")[0][0])
    recent = [dict(ts=float(r[0]), feeler=int(r[1]), feeler_name=r[2], about=int(r[3]), about_name=r[4],
                   delta=round(float(r[5]), 1), score=round(float(r[6]), 1), source=r[7], reason="\t".join(r[8:]))
              for r in sql("SELECT UNIX_TIMESTAMP(l.ts), l.bot_guid, b.name, l.other_guid, o.name, l.delta, l.score, "
                           "l.source, l.reason FROM regard_log l JOIN characters b ON b.guid = l.bot_guid "
                           "JOIN characters o ON o.guid = l.other_guid ORDER BY l.id DESC LIMIT 60")]
    # Every tie, not a top slice: the inspector shows a person's whole standing, and a well-travelled
    # bot has upwards of 150 of them. They go to one file per person (write_ties) so that regard.json,
    # which every open dashboard re-reads every 30 s, stays a light index.
    rows = sql("SELECT r.bot_guid, b.name, r.other_guid, o.name, r.score, r.familiarity, "
               "(opk.kind IN ('bot', 'alt')), COALESCE(r.description, '') FROM regard r "
               "JOIN characters b ON b.guid = r.bot_guid JOIN characters o ON o.guid = r.other_guid "
               "JOIN person_kind opk ON opk.guid = o.guid "
               "ORDER BY ABS(r.score) DESC")
    ties = [dict(feeler=int(r[0]), feeler_name=r[1], about=int(r[2]), about_name=r[3], score=round(float(r[4]), 1),
                 familiarity=int(r[5]), about_bot=r[6] == "1", words=words(float(r[4])), description="\t".join(r[7:]))
            for r in rows]
    everyone = {}
    for t in ties:   # the rows arrive strongest-feeling first, so each list keeps that order
        everyone.setdefault(t["feeler"], dict(name=t["feeler_name"], bot=True, feels=[], felt_by=[]))["feels"].append(t)
        everyone.setdefault(t["about"], dict(name=t["about_name"], bot=t["about_bot"], feels=[], felt_by=[]))["felt_by"].append(t)
    for p in everyone.values():
        p["bot"] = p["bot"] and bool(p["feels"]) or p["bot"]
    write_ties(everyone)
    # Plan 18 P8c: the latest conversations bots had with no player about.
    talks = [dict(ts=float(r[0]), place=r[1], a=r[2], b=r[3], exchange=json.loads(r[4]), moments=json.loads("\t".join(r[5:])))
             for r in sql("SELECT UNIX_TIMESTAMP(t.ts), t.place, a.name, b.name, t.exchange, t.moments FROM bot_talk t "
                          "JOIN characters a ON a.guid = t.a_guid JOIN characters b ON b.guid = t.b_guid "
                          "ORDER BY t.id DESC LIMIT 15")]
    generated = time.time()
    totals = write_archives(ties, generated)
    out = dict(generated=generated, pairs=pairs, moments_day=moments_day, recent=recent, talks=talks,
               totals=totals,
               warmest=sorted(ties, key=lambda t: -t["score"])[:20],
               coldest=sorted(ties, key=lambda t: t["score"])[:20],
               people={str(g): dict(name=p["name"], bot=p["bot"], n_feels=len(p["feels"]),
                                    n_felt_by=len(p["felt_by"])) for g, p in everyone.items()})
    write_json(SNAPSHOT, out)
    return len(everyone)


def chat_snapshot():
    """The party lines said in a company, for the dashboard's Companies panel.

    A company leaves no trace in ledger_chat: chat_type 2 carries no group id, and listener_guid is NULL
    for every bot-to-bot party line by design -- mod-ledger only fills it from NearestRealPlayer. So this
    is a flat record keyed by speaker, and the dashboard, which learns who stands with whom from /bots,
    assembles each company's history itself. Oldest first, the way it is read.

    Party talk arrives under two types, and taking only 2 (CHAT_MSG_PARTY) silently dropped the player:
    a bot is always a member and speaks as 2, while the leader speaks as 51 (CHAT_MSG_PARTY_LEADER), and
    the leader of the company the player rides with is the player. Every 51 row in the ledger was the main,
    593 lines of him, absent from his own company's card. TYPE_CLASS above already counts 51 as group talk;
    this query was the one place that did not.
    """
    rows = sql("SELECT id, UNIX_TIMESTAMP(ts), speaker_guid, speaker_name, zone_id, map_id, text "
               "FROM ledger_chat WHERE chat_type IN (2, 51) "
               f"ORDER BY id DESC LIMIT {CHAT_SNAPSHOT_LINES}")
    lines = [dict(id=int(r[0]), ts=float(r[1]), guid=int(r[2]), name=r[3], zone=int(r[4]), map=int(r[5]),
                  text="\t".join(r[6:]))
             for r in reversed(rows)]
    out = dict(generated=time.time(), lines=lines)
    write_json(CHAT_SNAPSHOT, out)
    return len(lines)


def memories_snapshot():
    """What the bots still carry, for the dashboard's Memories panel (plans/38).

    The store is mod-ollama-chat's, not ours -- we only read it. It lives here rather than in
    services/memory/recall.py, which owns the table, because this loop already has the database helper,
    the atomic writer and a unit that runs every two minutes; recall.py is a hand-run CLI with none of them.

    What the panel can honestly show is who remembered, what, how much it weighed and when. NOT where:
    the table is (bot_guid, memory_text, importance, created_at) and nothing else, so a place exists only
    as words inside the prose. Regexing it back out would be guesswork, so the panel says time alone.
    """
    rows = sql("SELECT m.id, m.bot_guid, c.name, m.importance, UNIX_TIMESTAMP(m.created_at), "
               "REPLACE(REPLACE(m.memory_text, '\\t', ' '), '\\n', ' ') "
               "FROM mod_ollama_chat_memories m JOIN characters c ON c.guid = m.bot_guid "
               f"ORDER BY m.id DESC LIMIT {MEMORIES_SNAPSHOT_ROWS}")
    lines = [dict(id=int(r[0]), guid=int(r[1]), name=r[2], importance=int(r[3]), ts=float(r[4]),
                  text="\t".join(r[5:]))
             for r in rows]

    totals = sql("SELECT COUNT(*), COUNT(DISTINCT bot_guid), ROUND(AVG(importance), 1), "
                 "SUM(created_at > NOW() - INTERVAL 1 DAY) FROM mod_ollama_chat_memories")
    total, bots, avg, day = (totals[0] if totals else ["0", "0", "0", "0"])

    # Who carries the most. The deepest stores are the bots someone has actually travelled with, which is
    # the whole point of the panel: it shows the society's memory pooling around the people in it.
    top = [dict(guid=int(r[0]), name=r[1], held=int(r[2])) for r in
           sql("SELECT m.bot_guid, c.name, COUNT(*) AS held FROM mod_ollama_chat_memories m "
               "JOIN characters c ON c.guid = m.bot_guid "
               "GROUP BY m.bot_guid, c.name ORDER BY held DESC, c.name LIMIT 12")]

    # Every memory, every bot: the whole store, for the per-person tab and the full-screen view. One query
    # feeds all three files -- the head the panel shows, one file per person, and the archive.
    everything = sql("SELECT m.bot_guid, c.name, m.importance, UNIX_TIMESTAMP(m.created_at), "
                     "REPLACE(REPLACE(m.memory_text, '\\t', ' '), '\\n', ' ') "
                     "FROM mod_ollama_chat_memories m JOIN characters c ON c.guid = m.bot_guid "
                     "ORDER BY m.importance DESC, m.id DESC")
    held, names = {}, {}
    for r in everything:
        guid = int(r[0])
        names[guid] = r[1]
        held.setdefault(guid, []).append([int(r[2]), float(r[3]), "\t".join(r[4:])])

    write_bot_memories(held, names)

    # Arrays beside a name table, the way the feelings archives are built: as objects this file doubles,
    # and it grows every two minutes for as long as the realm runs.
    write_json(MEMORIES_ARCHIVE, dict(
        generated=time.time(),
        names={str(g): n for g, n in names.items()},
        rows=[[g, m[0], m[1], m[2]] for g, ms in held.items() for m in ms]))

    out = dict(generated=time.time(), lines=lines, top=top,
               counts={str(g): len(ms) for g, ms in held.items()},
               total=int(total or 0), bots=int(bots or 0), day=int(day or 0), avg=float(avg or 0))
    write_json(MEMORIES_SNAPSHOT, out)
    return len(lines)


_memories_written = {}   # guid -> digest of the file as last written; a cycle rewrites only what moved


def write_bot_memories(held, names):
    """One file per bot, holding everything they carry, for the inspector's Memories tab.

    Mirrors write_ties: the dashboard fetches a person's file only when their tab is opened, nothing in it
    is stamped with the time, and most bots gain no memory in a given cycle -- so rewriting only what moved
    matters more than the bytes. A bot whose memories were pruned away loses its file with the orphans.
    """
    os.makedirs(MEMORIES_DIR, exist_ok=True)
    for guid, ms in held.items():
        body = json.dumps(dict(guid=guid, name=names.get(guid, ""), memories=ms), separators=(",", ":"))
        digest = hashlib.blake2s(body.encode(), digest_size=8).hexdigest()
        path = os.path.join(MEMORIES_DIR, f"{guid}.json")
        if _memories_written.get(guid) == digest and os.path.exists(path):
            continue
        with open(path + ".tmp", "w") as f:
            f.write(body)
        os.replace(path + ".tmp", path)
        _memories_written[guid] = digest
    for name in os.listdir(MEMORIES_DIR):   # pruned away, the character is gone, or a stray .tmp
        guid = name[:-5]
        if name.endswith(".json") and guid.isdigit() and int(guid) in held:
            continue
        os.remove(os.path.join(MEMORIES_DIR, name))
        if guid.isdigit():
            _memories_written.pop(int(guid), None)


# ---------------------------------------------------------------------------
# 5c. journeys (plans/42): everything one character has done, gathered around them

def detail_of(text):
    """mod-ledger's detail column: JSON for most event types, empty for a few."""
    try:
        return json.loads(text) if text.startswith("{") else {}
    except ValueError:
        return {}


def journey_talks(lines, parties, add):
    """Party chat, gathered into the conversations each speaker was actually part of.

    ledger_chat carries no group id -- listener_guid is NULL for every bot-to-bot party line by design,
    and chat_snapshot above says why -- so a conversation cannot be read off a row. What can be done
    honestly is to take one person's own party lines, cut them wherever they fall silent for longer than
    JOURNEY_TALK_GAP, ask Parties who stood with them while each stretch ran, and gather those
    companions' lines from the same stretch. The result is the talk as that person lived it: their own
    words beside the words they were answering, and nobody else's.

    A stretch with no companions is still kept. Group membership is replayed from a ledger that logs
    group_leave for some members and not others, so "nobody stood with them" means the record does not
    say, not that they were alone -- the view marks those rather than dropping them.
    """
    said_by = {}
    for ts, guid, zone, map_id, text in lines:
        said_by.setdefault(guid, []).append((ts, zone, map_id, text))
    for rows in said_by.values():
        rows.sort()
    stamps = {g: [r[0] for r in rows] for g, rows in said_by.items()}

    for guid, rows in said_by.items():
        spans = []
        for ts, zone, map_id, _ in rows:
            if spans and ts - spans[-1][1] <= JOURNEY_TALK_GAP:
                spans[-1][1] = ts
            else:
                spans.append([ts, ts, zone, map_id])
        for start, end, zone, map_id in spans:
            with_them = set()
            for t in (start, (start + end) / 2, end):
                with_them |= parties.standing_with(guid, t)
            with_them.discard(guid)
            exchange = []
            for speaker in {guid} | with_them:
                marks = stamps.get(speaker) or []
                lo = bisect.bisect_left(marks, start - JOURNEY_TALK_GAP)
                hi = bisect.bisect_right(marks, end + JOURNEY_TALK_GAP)
                for ts, _, _, text in (said_by.get(speaker) or [])[lo:hi]:
                    exchange.append([speaker, ts, text])
            exchange.sort(key=lambda line: line[1])
            add(guid, "talk", {"k": "talk", "ts": start, "until": end, "zone": zone, "map": map_id,
                               "with": sorted(with_them), "lines": exchange})


def journeys(force=False):
    """One file per character holding everything they have done, for the dashboard's Journey view.

    Five records go in, every one of them already written by something else: mod-ledger's deeds and
    party lines, regard_log's moments in both directions, bot_talk's conversations, and
    mod-ollama-chat's memories. Nothing here scores or decides anything -- these are the same rows the
    other panels show, gathered around one person and laid out in time.

    Mirrors write_ties and write_bot_memories: a file each, rewritten only when it moved, orphans swept.
    It runs on the chronicler's ten-minute cadence rather than this loop's two-minute one, because it
    reads whole tables rather than a cursor's worth, and what someone did yesterday does not change in
    the two minutes since it was last written.

    Returns the number of files rewritten, or None when it is not yet due.
    """
    now = int(time.time())
    if not force and now - cursor("journey") < JOURNEY_SECONDS:
        return None

    who = {int(r[0]): dict(name=r[1], race=int(r[2]), cls=int(r[3]), level=int(r[4]),
                           gender=int(r[5]), guild=int(r[6]), bot=r[7] == "1")
           for r in sql("SELECT c.guid, c.name, c.race, c.class, c.level, c.gender, "
                        "COALESCE(m.guildid, 0), pk.kind IN ('bot', 'alt') FROM characters c "
                        "JOIN person_kind pk ON pk.guid = c.guid "
                        "LEFT JOIN guild_member m ON m.guid = c.guid")}

    kept, span = {}, {}

    def add(guid, kind, entry):
        """One entry onto one person's pile. Entries arrive newest first, so capping is a slice."""
        if guid not in who:
            return
        kept.setdefault(guid, {}).setdefault(kind, []).append(entry)
        first, last = span.get(guid, (entry["ts"], entry["ts"]))
        span[guid] = (min(first, entry["ts"]), max(last, entry["ts"]))

    detail_sql = "REPLACE(REPLACE(COALESCE(detail, ''), '\\t', ' '), '\\n', ' ')"
    parties = Parties()

    # ---- deeds, and the comings and goings of a company ----
    kinds = ",".join(q(k) for k in JOURNEY_EVENTS)
    for r in sql(f"SELECT UNIX_TIMESTAMP(ts), actor_guid, event_type, COALESCE(subject_guid, 0), "
                 f"zone_id, map_id, {detail_sql} FROM ledger_event "
                 f"WHERE event_type IN ({kinds}) ORDER BY id DESC"):
        ts, guid, kind, subject = float(r[0]), int(r[1]), r[2], int(r[3])
        zone, map_id = int(r[4]), int(r[5])
        d = detail_of("\t".join(r[6:]))
        where = {"ts": ts, "zone": zone, "map": map_id}
        if kind == "group_join":
            # Who they joined, as the ledger had it at that moment; the leader is the row's subject.
            add(guid, "group", dict(where, k="group", t="join", leader=subject or guid,
                                    **{"with": sorted(parties.standing_with(guid, ts) - {guid})}))
        elif kind == "group_leave":
            # method 1 is a kick and names the kicker, 2 is walking away, and a disband ends the company.
            leaving = "disband" if d.get("disband") else ("kicked" if d.get("method") == 1 else "left")
            entry = dict(where, k="group", t=leaving)
            if d.get("kicker"):
                entry["by"] = int(d["kicker"])
            add(guid, "group", entry)
        else:
            entry = dict(where, k="deed", t=kind)
            if kind == "death":
                entry["foe"] = d.get("name") or ""
                if subject:
                    entry["by"] = subject
            elif kind == "quest_complete":
                entry["title"] = d.get("title") or ""
            elif kind == "level_up":
                entry["old"], entry["new"] = int(d.get("old") or 0), int(d.get("new") or 0)
            elif kind == "loot_item":
                entry["item"] = d.get("name") or ""
                entry["quality"] = int(d.get("quality") or 0)
                entry["count"] = int(d.get("count") or 1)
            elif kind == "zone_change":
                entry["from"], entry["to"] = int(d.get("from") or 0), int(d.get("to") or 0)
            elif kind == "pvp_kill" and subject:
                entry["by"] = subject
            add(guid, "deed", entry)

    # ---- the kills worth naming, and a tally of the rest ----
    for r in sql(f"SELECT UNIX_TIMESTAMP(ts), actor_guid, zone_id, map_id, {detail_sql} "
                 f"FROM ledger_event WHERE event_type = 'kill' AND {NOTABLE_KILL} ORDER BY id DESC"):
        d = detail_of("\t".join(r[4:]))
        add(int(r[1]), "deed", {"k": "deed", "t": "kill", "ts": float(r[0]), "zone": int(r[2]),
                                "map": int(r[3]), "foe": d.get("name") or "",
                                "rank": int(d.get("rank") or 0), "boss": 1 if d.get("boss") else 0,
                                "level": int(d.get("level") or 0)})

    bucket = JOURNEY_GRIND_BUCKET
    for r in sql(f"SELECT actor_guid, zone_id, map_id, FLOOR(UNIX_TIMESTAMP(ts) / {bucket}) * {bucket} h, "
                 f"COUNT(*) FROM ledger_event WHERE event_type = 'kill' AND NOT {NOTABLE_KILL} "
                 "GROUP BY actor_guid, zone_id, map_id, h ORDER BY h DESC"):
        add(int(r[0]), "grind", {"k": "grind", "ts": float(r[3]), "zone": int(r[1]),
                                 "map": int(r[2]), "n": int(r[4])})

    # ---- what was said in a company ----
    journey_talks([(float(r[0]), int(r[1]), int(r[2]), int(r[3]), "\t".join(r[4:]))
                   for r in sql("SELECT UNIX_TIMESTAMP(ts), speaker_guid, zone_id, map_id, "
                                "REPLACE(REPLACE(text, '\\t', ' '), '\\n', ' ') FROM ledger_chat "
                                "WHERE chat_type IN (2, 51) ORDER BY id")],
                  parties, add)

    # ---- conversations no one overheard (bot_talk); nothing of these was said in the game ----
    for r in sql("SELECT UNIX_TIMESTAMP(ts), a_guid, b_guid, place, zone_id, map_id, exchange, moments "
                 "FROM bot_talk ORDER BY id DESC"):
        ts, a, b = float(r[0]), int(r[1]), int(r[2])
        rest = dict(place=r[3], zone=int(r[4]), map=int(r[5]),
                    lines=json.loads(r[6]), moments=json.loads("\t".join(r[7:])))
        for me, other in ((a, b), (b, a)):
            add(me, "encounter", dict(rest, k="encounter", ts=ts, other=other))

    # ---- what passed between them and everyone else, both ways ----
    for r in sql("SELECT UNIX_TIMESTAMP(ts), bot_guid, other_guid, delta, score, source, "
                 "REPLACE(REPLACE(reason, '\\t', ' '), '\\n', ' ') FROM regard_log ORDER BY id DESC"):
        ts, feeler, about = float(r[0]), int(r[1]), int(r[2])
        moment = dict(k="feel", ts=ts, delta=round(float(r[3]), 1), score=round(float(r[4]), 1),
                      source=r[5], reason="\t".join(r[6:]))
        # "in" is what someone came away feeling about them; "out" is what they came away feeling.
        add(about, "feel", dict(moment, dir="in", other=feeler))
        add(feeler, "feel", dict(moment, dir="out", other=about))

    # ---- and what they still carry of it ----
    for r in sql("SELECT bot_guid, importance, UNIX_TIMESTAMP(created_at), "
                 "REPLACE(REPLACE(memory_text, '\\t', ' '), '\\n', ' ') "
                 "FROM mod_ollama_chat_memories ORDER BY id DESC"):
        add(int(r[0]), "memory", {"k": "memory", "ts": float(r[2]), "weight": int(r[1]),
                                  "text": "\t".join(r[3:])})

    return write_journeys(kept, span, who, now)


_journeys_written = {}   # guid -> digest of the file as last written; a cycle rewrites only what moved


def write_journeys(kept, span, who, now):
    """The files themselves, and the index the Journey panel lists people from."""
    os.makedirs(JOURNEYS_DIR, exist_ok=True)
    guilds = company_names()
    index, written, zones = [], 0, set()

    for guid, piles in kept.items():
        entries = []
        for kind, items in piles.items():
            entries.extend(items[:JOURNEY_MAX_PER_KIND])
        entries.sort(key=lambda e: -e["ts"])
        if not entries:
            continue

        # Everyone this record names, so the page can show a name without holding the whole roster.
        named = {guid}
        for e in entries:
            for key in ("other", "by", "leader"):
                if e.get(key):
                    named.add(int(e[key]))
            named.update(int(g) for g in e.get("with", []))
            named.update(int(line[0]) for line in e.get("lines", []) if isinstance(line[0], int))
            # Where it happened, including both ends of a zone_change, so the index can name them all.
            zones.update(int(e[key]) for key in ("zone", "from", "to") if e.get(key))

        p = who[guid]
        first, last = span[guid]
        body = json.dumps(dict(
            guid=guid, name=p["name"], bot=p["bot"], race=p["race"], cls=p["cls"], level=p["level"],
            gender=p["gender"], guild=p["guild"], guild_name=guilds.get(p["guild"], ""),
            first=first, last=last, kept=len(entries),
            counts={k: len(v) for k, v in piles.items()},
            names={str(g): who[g]["name"] for g in named if g in who},
            entries=entries), separators=(",", ":"))

        digest = hashlib.blake2s(body.encode(), digest_size=8).hexdigest()
        path = os.path.join(JOURNEYS_DIR, f"{guid}.json")
        if _journeys_written.get(guid) != digest or not os.path.exists(path):
            with open(path + ".tmp", "w") as f:
                f.write(body)
            os.replace(path + ".tmp", path)
            _journeys_written[guid] = digest
            written += 1

        index.append(dict(guid=guid, name=p["name"], bot=p["bot"], race=p["race"], cls=p["cls"],
                          level=p["level"], guild=p["guild"], n=len(entries), first=first, last=last,
                          counts={k: len(v) for k, v in piles.items()}))

    for name in os.listdir(JOURNEYS_DIR):   # the character is gone, or a stray .tmp
        guid = name[:-5]
        if name.endswith(".json") and guid.isdigit() and int(guid) in kept:
            continue
        os.remove(os.path.join(JOURNEYS_DIR, name))
        if guid.isdigit():
            _journeys_written.pop(int(guid), None)

    index.sort(key=lambda p: -p["n"])
    # Every place these records name. The dashboard cannot do this for itself: /worldmap carries only
    # the WorldMapArea rows, which name no dungeon interior, and map_id is no help either -- it is 0
    # on 9,503 of 14,498 chat rows and on a third of the events inside the Deadmines. The zone id is
    # the one field that is always right, so the names come from AreaTable.dbc, with lands.py's
    # wording preferred where it has some ("the Barrens" reads better than "The Barrens").
    known = areas.load()

    def place(zone):
        return LAND[zone][1] if zone in LAND else known.get(zone, "")

    write_json(JOURNEYS_SNAPSHOT, dict(generated=time.time(), people=index,
                                       places={str(z): place(z) for z in sorted(zones) if place(z)},
                                       instances={str(m): n for m, n in lands.INSTANCE_NAMES.items()}))
    sql(cursor_sql("journey", now), fetch=False)
    return written


# ---------------------------------------------------------------------------
# 6. companies (plan 14 B2): deeds -> influence -> holdings; incidents move stances

def land_of(zone_id, map_id):
    return lands.INSTANCE_LANDS.get(map_id) or (zone_id if zone_id in LAND else 0)


def instance_name(map_id):
    """The name of the dungeon a deed was done in, or None out in the world."""
    return lands.INSTANCE_NAMES.get(int(map_id or 0))


class Parties:
    """Who stood with whom, replayed from group_join / group_leave. This loop keeps the same thing live in
    GROUPS while it runs; anything on its own cadence -- the chronicler asking who was there when a
    stronghold fell -- has to rebuild it, because a bot's kill credit names only the one who struck."""

    def __init__(self, until=None):
        where = f" AND ts < {q(until.strftime('%Y-%m-%d %H:%M:%S'))}" if until else ""
        rows = sql("SELECT UNIX_TIMESTAMP(ts), actor_guid, COALESCE(subject_guid, 0), event_type, "
                   "COALESCE(detail, '') "
                   f"FROM ledger_event WHERE event_type IN ('group_join', 'group_leave'){where} ORDER BY id")
        self.stamps, self.marks, groups = [], [], {}
        for r in rows:
            ts, actor, subject, kind = float(r[0]), int(r[1]), int(r[2]), r[3]
            detail = "\t".join(r[4:])
            if kind == "group_join" and (not subject or subject == actor):
                # A leader founding a company. This is the only reliable reset: group_leave is logged for
                # some members and not others, so replaying leaves alone leaves every past companion in the
                # party for ever (eleven names stood with the player in the Deadmines; five were there).
                groups[actor] = {actor}
            elif kind == "group_join":
                groups.setdefault(subject, {subject}).add(actor)
            else:
                for members in groups.values():
                    members.discard(actor)
                if json.loads(detail or "{}").get("disband"):
                    groups.pop(actor, None)
                groups = {ldr: m for ldr, m in groups.items() if len(m) > 1}
            standing = {}
            for members in groups.values():
                party = frozenset(members)
                for g in members:
                    standing[g] = party
            self.stamps.append(ts)
            self.marks.append(standing)

    def standing_with(self, guid, ts):
        """Everyone in `guid`'s company at that moment, `guid` included; just them if they stood alone."""
        i = bisect.bisect_right(self.stamps, float(ts)) - 1
        if i < 0:
            return {int(guid)}
        return set(self.marks[i].get(int(guid), {int(guid)}))


def company_names():
    return {int(r[0]): r[1] for r in sql("SELECT guildid, name FROM guild")}


class Incidents:
    def __init__(self):
        self.items = []   # (guild_a, guild_b or 0, zone, kind, detail, stance delta)

    def add(self, a, b, zone, kind, detail, delta=0.0):
        self.items.append((int(a), int(b or 0), int(zone or 0), kind, detail[:255], float(delta)))


RECENT_PREY = {}   # (creature entry, land) -> [(ts, guildid)]; kept while the service runs


def process_companies(incidents, names):
    """Members' deeds since the cursor. Returns (last_id, rows, {(guildid, land): points},
    {(guildid, real player): points} for the rank ladder)."""
    cur = cursor("company")
    rows = sql("SELECT e.id, UNIX_TIMESTAMP(e.ts), e.event_type, e.zone_id, e.map_id, m.guildid, "
               "COALESCE(sm.guildid, 0), e.actor_guid, COALESCE(e.detail, '') FROM ledger_event e "
               "JOIN guild_member m ON m.guid = e.actor_guid LEFT JOIN guild_member sm ON sm.guid = e.subject_guid "
               f"WHERE e.id > {cur} AND e.event_type IN ('kill', 'quest_complete', 'duel_won') "
               f"ORDER BY e.id LIMIT {EVENT_BATCH}")
    seats = {(int(r[0]), int(r[1])) for r in sql("SELECT guildid, zone_id FROM guild_seat")}
    real = {int(r[0]) for r in sql("SELECT c.guid FROM characters c JOIN person_kind pk ON pk.guid = c.guid "
                                   "WHERE pk.kind IN ('main', 'player')")}
    points, player_points = {}, {}
    last = cur
    for r in rows:
        lid, ts, kind, gid, other, actor = int(r[0]), float(r[1]), r[2], int(r[5]), int(r[6]), int(r[7])
        land = land_of(int(r[3]), int(r[4]))
        last = lid
        detail = "\t".join(r[8:])
        try:
            d = json.loads(detail) if detail.startswith("{") else {}
        except ValueError:
            d = {}
        if kind == "duel_won":
            if other and other != gid:
                incidents.add(gid, other, land, "duel",
                              f"{names.get(other, 'a company')} lost a duel to {names.get(gid, 'a company')}",
                              INCIDENT_STANCE["duel"])
            continue
        if not land:
            continue
        deed = "quest" if kind == "quest_complete" else (
            "dungeon_boss" if d.get("boss") else KILL_RANK.get(int(d.get("rank") or 0), "kill"))
        key = (gid, land)
        gained = INFLUENCE_POINTS[deed] * (SEAT_SHARE if key in seats else 1.0)
        points[key] = points.get(key, 0.0) + gained
        if actor in real:
            player_points[(gid, actor)] = player_points.get((gid, actor), 0.0) + gained
        if deed in ("rare", "rare_elite", "world_boss", "dungeon_boss") and d.get("entry"):
            hunters = [h for h in RECENT_PREY.get((d["entry"], land), []) if ts - h[0] <= SAME_PREY_SECONDS]
            for other_gid in sorted({g for _, g in hunters if g != gid}):
                incidents.add(gid, other_gid, land, "same_prey",
                              f"{names.get(gid, 'A company')} brought down {str(d.get('name') or 'a great beast')[:60]} "
                              f"in {LAND[land][1]} while {names.get(other_gid, 'another company')} were on its trail",
                              INCIDENT_STANCE["same_prey"])
            RECENT_PREY[(d["entry"], land)] = hunters + [(ts, gid)]
    return last, len(rows), points, player_points


def commit_influence(points, extra_sql, player_points=None):
    stmts = ["START TRANSACTION;",
             # A new seat starts with a standing claim; IGNORE keeps influence already earned there.
             f"INSERT IGNORE INTO guild_influence (guildid, zone_id, influence) SELECT guildid, zone_id, {SEAT_START} "
             "FROM guild_seat;"]
    items = sorted(points.items())
    for part in chunks(items, 300):
        stmts.append("INSERT INTO guild_influence (guildid, zone_id, influence) VALUES "
                     + ",".join(f"({g},{z},{p:.2f})" for (g, z), p in part)
                     + " ON DUPLICATE KEY UPDATE influence = influence + VALUES(influence), updated_at = NOW();")
    # Plan 18 P4: what each real player's deeds brought their company, for the rank ladder. Never decays.
    for part in chunks(sorted((player_points or {}).items()), 300):
        stmts.append("INSERT INTO company_deeds (guildid, player_guid, influence) VALUES "
                     + ",".join(f"({g},{p},{v:.2f})" for (g, p), v in part)
                     + " ON DUPLICATE KEY UPDATE influence = influence + VALUES(influence), updated_at = NOW();")
    stmts += [extra_sql, "COMMIT;"]
    sql("\n".join(stmts), fetch=False)
    return len(items)


def words_incidents(chat, incidents, names):
    """Judged moments between members of two different companies become incidents between the companies."""
    for speaker, target, feels, why, line in chat.words:
        a, b = speaker["guild"], target["guild"]
        if not a or not b or a == b or abs(feels) < WORDS_MIN:
            continue
        verb = "won the regard of" if feels > 0 else "gave offence to"
        incidents.add(a, b, land_of(line["zone"], line["map"]), "words",
                      f"{speaker['name']} of {names.get(a, 'a company')} {verb} {target['name']} of "
                      f"{names.get(b, 'a company')}: {why}", feels * WORDS_STANCE_SHARE)


def decay_influence():
    floor = f"IF(s.guildid IS NULL, 0, {SEAT_FLOOR})"
    sql("UPDATE guild_influence i LEFT JOIN guild_seat s ON s.guildid = i.guildid AND s.zone_id = i.zone_id "
        # MySQL applies SET left to right: influence still sees the old decayed_at.
        f"SET i.influence = {floor} + GREATEST(0, i.influence - {floor}) * "
        f"POW({INFLUENCE_KEEP_PER_WEEK}, TIMESTAMPDIFF(MINUTE, i.decayed_at, NOW()) / 10080.0), i.decayed_at = NOW() "
        "WHERE i.decayed_at < NOW() - INTERVAL 1 HOUR; "
        "DELETE i FROM guild_influence i LEFT JOIN guild_seat s ON s.guildid = i.guildid AND s.zone_id = i.zone_id "
        "WHERE s.guildid IS NULL AND i.influence < 1;", fetch=False)


def reckon(incidents, names):
    """Once an hour: who holds each land and who contests it. Returns lands reckoned, or None if not due."""
    now = int(time.time())
    if now - cursor("reckon") < RECKON_SECONDS:
        return None
    ranked = {}
    for zone, gid, influence in sql("SELECT zone_id, guildid, influence FROM guild_influence ORDER BY zone_id, influence DESC"):
        ranked.setdefault(int(zone), []).append((int(gid), float(influence)))
    held = {int(r[0]): (int(r[1]), int(r[2]))
            for r in sql("SELECT zone_id, holder, COALESCE(challenger, 0) FROM guild_holding")}
    # The first reckoning only records how things stand: nobody took anything from anybody.
    first = not held
    stance = (lambda kind: 0.0) if first else (lambda kind: INCIDENT_STANCE.get(kind, 0.0))
    name = lambda g: names.get(g, "a company")
    stmts = ["START TRANSACTION;"]
    for zone, land in LAND.items():
        influence = dict(ranked.get(zone, []))
        standing = [(g, i) for g, i in ranked.get(zone, []) if i >= HOLD_MIN]
        old, old_challenger = held.get(zone, (0, 0))
        holder, taken = old, False
        if not standing:
            holder = 0
            if old:
                incidents.add(old, 0, zone, "released", f"{name(old)} no longer hold {land[1]}")
        elif not old or influence.get(old, 0) < HOLD_MIN:
            holder = standing[0][0]
            if old:
                incidents.add(holder, old, zone, "taken", f"{name(holder)} took {land[1]} from {name(old)}", stance("taken"))
            else:
                incidents.add(holder, 0, zone, "claimed", f"{name(holder)} now hold {land[1]}")
            taken = True
        elif standing[0][0] != old and standing[0][1] >= influence[old] * (1 + TAKE_MARGIN):
            holder = standing[0][0]
            incidents.add(holder, old, zone, "taken", f"{name(holder)} took {land[1]} from {name(old)}", stance("taken"))
            taken = True
        if not holder:
            stmts.append(f"DELETE FROM guild_holding WHERE zone_id = {zone};")
            continue
        challenger = next((g for g, i in standing if g != holder and i >= influence[holder] * (1 - CONTEST_MARGIN)), 0)
        if challenger and not taken and challenger != old_challenger:
            incidents.add(challenger, holder, zone, "contested",
                          f"{name(challenger)} are pushing into {land[1]}, held by {name(holder)}", stance("contested"))
        elif old_challenger and not challenger and not taken and holder == old:
            incidents.add(holder, old_challenger, zone, "held", f"{name(holder)} kept {land[1]} against {name(old_challenger)}")
        since = "NOW()" if holder != old else "holder_since"
        stmts.append(f"INSERT INTO guild_holding (zone_id, holder, challenger) VALUES ({zone}, {holder}, "
                     f"{challenger or 'NULL'}) ON DUPLICATE KEY UPDATE holder_since = {since}, "
                     "holder = VALUES(holder), challenger = VALUES(challenger), updated_at = NOW();")
    stmts += [cursor_sql("reckon", now), "COMMIT;"]
    sql("\n".join(stmts), fetch=False)
    return len(LAND)


def commit_incidents(incidents, era):
    """Store incidents (a pair's repeats inside the cooldown are dropped) and move stances. Returns the count."""
    if not incidents.items:
        return 0
    last = {(int(r[0]), int(r[1]), r[2]): float(r[3]) for r in sql(
        "SELECT LEAST(guild_a, guild_b), GREATEST(guild_a, guild_b), kind, UNIX_TIMESTAMP(MAX(ts)) FROM guild_incident "
        "WHERE ts > NOW() - INTERVAL 1 DAY AND guild_b IS NOT NULL GROUP BY 1, 2, 3")}
    now = time.time()
    kept, moves = [], {}
    for a, b, zone, kind, detail, delta in incidents.items:
        pair = (min(a, b), max(a, b))
        hours = INCIDENT_COOLDOWN_HOURS.get(kind)
        if b and hours:
            if now - last.get(pair + (kind,), 0) < hours * 3600:
                continue
            last[pair + (kind,)] = now
        kept.append((a, b, zone, kind, detail, delta))
        if b and delta:
            moves[pair] = moves.get(pair, 0.0) + delta
    stmts = ["START TRANSACTION;"]
    for part in chunks(kept, 200):
        stmts.append("INSERT INTO guild_incident (guild_a, guild_b, zone_id, kind, detail, stance_delta) VALUES "
                     + ",".join(f"({a},{b or 'NULL'},{zone or 'NULL'},{q(kind)},{q(detail)},{delta:.2f})"
                                for a, b, zone, kind, detail, delta in part) + ";")
    for (a, b), delta in sorted(moves.items()):
        # Companies with no seeded relation get a bare one: the stance alone, no story yet.
        stmts.append("INSERT INTO guild_relation (guild_a, guild_b, stance, disposition, origin, moments, a_says, b_says, "
                     f"model, era, last_incident_at) VALUES ({a}, {b}, {max(-100.0, min(100.0, delta)):.2f}, 'strangers', "
                     f"'', '[]', '', '', 'incidents', {q(era)}, NOW()) ON DUPLICATE KEY UPDATE "
                     "stance = GREATEST(-100, LEAST(100, stance + VALUES(stance))), last_incident_at = NOW();")
    stmts.append("COMMIT;")
    sql("\n".join(stmts), fetch=False)
    if moves:
        refresh_guild_baselines()
    return len(kept)


def stance_words(stance):
    if stance <= -60:
        return "blood feud"
    if stance <= -30:
        return "rivals"
    if stance <= -10:
        return "wary"
    if stance < 10:
        return "indifferent"
    if stance < 30:
        return "on good terms"
    if stance < 60:
        return "friends"
    return "sworn allies"


def companies_snapshot():
    horde = ",".join(map(str, sorted(HORDE)))
    out = dict(generated=time.time(), companies={}, lands={}, relations=[], incidents=[], candidacy=[], ended=[])
    for r in sql("SELECT ended_id, guildid, cause, JSON_LENGTH(members), UNIX_TIMESTAMP(ended_at), name FROM company_ended "
                 "ORDER BY ended_id DESC LIMIT 20"):
        out["ended"].append(dict(ended_id=int(r[0]), guildid=int(r[1]), cause=r[2], members=int(r[3]),
                                 ended_at=float(r[4]), name="\t".join(r[5:])))
    # Plan 18 P4: the title a member holds and the action waiting to be carried out, if any.
    for r in sql("SELECT k.guildid, k.player_guid, p.name, k.stage, COALESCE(k.sponsor_guid, 0), COALESCE(s.name, ''), "
                 "k.vouchers, k.standing, COALESCE(b.name, ''), UNIX_TIMESTAMP(k.since), COALESCE(gr.rname, ''), "
                 "COALESCE((SELECT a.action FROM company_action a WHERE a.guildid = k.guildid "
                 "AND a.player_guid = k.player_guid AND a.done_at IS NULL ORDER BY a.id DESC LIMIT 1), '') "
                 "FROM company_candidacy k "
                 "JOIN characters p ON p.guid = k.player_guid LEFT JOIN characters s ON s.guid = k.sponsor_guid "
                 "LEFT JOIN characters b ON b.guid = k.blackball_guid "
                 "LEFT JOIN guild_member gm ON gm.guid = k.player_guid AND gm.guildid = k.guildid "
                 "LEFT JOIN guild_rank gr ON gr.guildid = gm.guildid AND gr.rid = gm.`rank` "
                 "ORDER BY FIELD(k.stage, 'joined', 'offered', 'spoken_for', 'noticed'), k.standing DESC"):
        out["candidacy"].append(dict(guildid=int(r[0]), player=int(r[1]), player_name=r[2], stage=r[3], sponsor=int(r[4]),
                                     sponsor_name=r[5], vouchers=int(r[6]), standing=round(float(r[7]), 1),
                                     blackball_name=r[8], since=float(r[9]), rank_name=r[10], pending=r[11]))
    for gid, name, horde_n, n in sql(f"SELECT g.guildid, g.name, SUM(c.race IN ({horde})), COUNT(*) FROM guild g "
                                     "JOIN guild_member m ON m.guildid = g.guildid JOIN characters c ON c.guid = m.guid "
                                     "GROUP BY g.guildid"):
        out["companies"][gid] = dict(name=name, faction="Horde" if 2 * int(horde_n) > int(n) else "Alliance",
                                     seats=[], holds=[], contests=[])
    for gid, band, zone, zone_name, hold in sql("SELECT guildid, band, zone_id, zone_name, hold FROM guild_seat "
                                                "ORDER BY guildid, FIELD(band, 'recruits', 'blooded', 'seasoned')"):
        if gid in out["companies"]:
            out["companies"][gid]["seats"].append(dict(band=band, zone=int(zone), zone_name=zone_name, hold=hold))
    influence = {}
    for zone, gid, value in sql("SELECT zone_id, guildid, influence FROM guild_influence ORDER BY influence DESC"):
        influence.setdefault(int(zone), []).append([int(gid), round(float(value))])
    holding = {int(r[0]): (int(r[1]), int(r[2]), float(r[3]))
               for r in sql("SELECT zone_id, holder, COALESCE(challenger, 0), UNIX_TIMESTAMP(holder_since) FROM guild_holding")}
    for zone, land in LAND.items():
        holder, challenger, since = holding.get(zone, (0, 0, 0.0))
        out["lands"][str(zone)] = dict(name=land[1], levels=[land[2], land[3]], lands=land[4], holder=holder,
                                       challenger=challenger, since=since, influence=influence.get(zone, [])[:5])
        if str(holder) in out["companies"]:
            out["companies"][str(holder)]["holds"].append(zone)
        if str(challenger) in out["companies"]:
            out["companies"][str(challenger)]["contests"].append(zone)
    for r in sql(f"SELECT guild_a, guild_b, stance, disposition, origin, moments, a_says, b_says, "
                 "COALESCE(UNIX_TIMESTAMP(last_incident_at), 0) FROM guild_relation ORDER BY stance"):
        out["relations"].append(dict(a=int(r[0]), b=int(r[1]), stance=round(float(r[2]), 1), words=stance_words(float(r[2])),
                                     disposition=r[3], origin=r[4], moments=json.loads(r[5] or "[]"), a_says=r[6],
                                     b_says=r[7], last_incident=float(r[8])))
    for r in sql("SELECT UNIX_TIMESTAMP(ts), guild_a, COALESCE(guild_b, 0), COALESCE(zone_id, 0), kind, stance_delta, detail "
                 "FROM guild_incident ORDER BY id DESC LIMIT 60"):
        out["incidents"].append(dict(ts=float(r[0]), a=int(r[1]), b=int(r[2]), zone=int(r[3]), kind=r[4],
                                     delta=round(float(r[5]), 1), detail="\t".join(r[6:])))
    write_json(COMPANIES_SNAPSHOT, out)
    return len(out["incidents"])


# ---------------------------------------------------------------------------
# 7. words (plan 14 B3): what members know of their company, and what is said in each land. No numbers.

COMPANY_TALK = {"blood feud": "are in a blood feud", "rivals": "are bitter rivals", "wary": "are wary of each other",
                "on good terms": "are on good terms", "friends": "are friends", "sworn allies": "are sworn allies"}
WORDS_LANDS = 4          # lands named in one sentence before "and elsewhere"
WORDS_RECENT_DAYS = 3


# Lands spoken of with "the" ("in the Badlands"), besides those named "The ...".
LANDS_WITH_THE = {3, 4, 8, 11, 28, 36, 44, 45, 46, 139, 267, 406}


def land_name(zone):
    name = LAND[zone][1]
    if name.startswith("The "):
        return "the " + name[4:]
    return "the " + name if zone in LANDS_WITH_THE else name


def deed_place(zone, map_id):
    """Where a deed was done, as a scribe would say it. A dungeon counts toward the land above it for
    influence and for where a rumour spreads (land_of), but it has a name of its own -- and losing that name
    made every deed in the Deadmines read "in Westfall", so no scribe could say a stronghold had been
    cleared at all (plans/30 §6)."""
    inside = instance_name(map_id)
    if inside:
        return inside
    land = land_of(zone, map_id)
    return land_name(land) if land else ""


def join_names(items, cap=WORDS_LANDS):
    items = list(items)
    if len(items) > cap:
        items = items[:cap] + ["elsewhere"]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def company_words():
    names = company_names()
    seats = {}
    for gid, zone in sql("SELECT guildid, zone_id FROM guild_seat ORDER BY guildid, FIELD(band, 'recruits', 'blooded', 'seasoned')"):
        if int(zone) in LAND:
            seats.setdefault(int(gid), []).append(land_name(int(zone)))
    holding = [(int(r[0]), int(r[1]), int(r[2])) for r in sql("SELECT zone_id, holder, COALESCE(challenger, 0) FROM guild_holding")
               if int(r[0]) in LAND]
    relations = [(int(r[0]), int(r[1]), float(r[2]), r[3], "\t".join(r[4:]))
                 for r in sql("SELECT guild_a, guild_b, stance, a_says, b_says FROM guild_relation")]
    recent = [(int(r[0]), int(r[1]), "\t".join(r[2:])) for r in sql(
        "SELECT guild_a, COALESCE(guild_b, 0), detail FROM guild_incident "
        f"WHERE kind <> 'claimed' AND ts > NOW() - INTERVAL {WORDS_RECENT_DAYS} DAY ORDER BY id DESC")]

    guild_rows = []
    for gid, name in sorted(names.items()):
        parts = [f"Your company, {name}, keeps seats in {join_names(seats[gid])}." if gid in seats
                 else f"You are sworn to the company {name}."]
        holds = [land_name(z) for z, h, c in holding if h == gid and not c]
        if holds:
            parts.append(f"It holds {join_names(holds)}.")
        for z, h, c in [x for x in holding if x[1] == gid and x[2]][:2]:
            parts.append(f"It holds {land_name(z)}, but {names.get(c, 'another company')} are pushing to take it.")
        for z, h, c in [x for x in holding if x[2] == gid][:2]:
            parts.append(f"It is pushing into {land_name(z)}, held by {names.get(h, 'another company')}.")
        mine = sorted(((stance, b if a == gid else a, a_says if a == gid else b_says)
                       for a, b, stance, a_says, b_says in relations if gid in (a, b)), key=lambda r: r[0])
        foe = next((r for r in mine if stance_words(r[0]) in ("blood feud", "rivals", "wary")), None)
        if foe:
            line = f"Your company and {names.get(foe[1], 'another company')} {COMPANY_TALK[stance_words(foe[0])]}"
            says = foe[2].strip()
            if says:
                says += "" if says[-1] in ".!?" else "."
                parts.append(f"{line}; your people say of them: \"{says}\"")
            else:
                parts.append(line + ".")
        friend = next((r for r in reversed(mine) if stance_words(r[0]) in ("on good terms", "friends", "sworn allies")), None)
        if friend:
            parts.append(f"Your company and {names.get(friend[1], 'another company')} {COMPANY_TALK[stance_words(friend[0])]}.")
        last = next((d for a, b, d in recent if gid in (a, b)), None)
        if last:
            parts.append(f"Lately: {last}.")
        guild_rows.append((gid, " ".join(parts)[:1000]))

    land_rows = []
    for z, h, c in holding:
        line = f"In {land_name(z)}, {names.get(h, 'a company')} hold sway"
        land_rows.append((z, (line + (f", and {names[c]} are pushing to take it" if c in names else "") + ".")[:255]))

    stmts = ["START TRANSACTION;", "DELETE FROM guild_words;", "DELETE FROM land_words;"]
    if guild_rows:
        stmts.append("INSERT INTO guild_words (guildid, words) VALUES "
                     + ",".join(f"({g},{q(w)})" for g, w in guild_rows) + ";")
    if land_rows:
        stmts.append("INSERT INTO land_words (zone_id, words) VALUES "
                     + ",".join(f"({z},{q(w)})" for z, w in land_rows) + ";")
    stmts.append("COMMIT;")
    sql("\n".join(stmts), fetch=False)
    return len(guild_rows), len(land_rows)


# ---------------------------------------------------------------------------

def cycle(args, judge, meta):
    if os.path.exists(PAUSE_FILE):
        log("paused (PAUSE file present)")
        return
    sql("".join(open(os.path.join(HERE, f)).read() for f in ("regard_tables.sql", "rivalry_tables.sql", "company_tables.sql")),
        fetch=False)
    # Before anything reads a guild id: a dead company's rows must be archived before a newcomer can inherit them.
    born, died = lifecycle()
    load_relations()

    events = Changes()
    event_last, event_rows = process_events(events)
    n_events = commit(events, people(events.guids()), cursor_sql("event", event_last))

    answers = Changes()
    voiced = []   # plan 18 P6: refusals the bots will whisper, stored with the company actions below
    answer_last, answer_rows = process_answers(answers, voiced)
    n_answers = commit(answers, people(answers.guids()), cursor_sql("answer", answer_last))

    positions = fetch_positions() or {}   # plan 18 P8a/P8c: the dashboard's snapshot, read once a cycle
    together = Changes()
    n_presence = presence(together, positions)
    commit(together, people(together.guids()), log_moments=False)

    names = company_names()
    incidents = Incidents()
    company_last, deeds, points, player_points = process_companies(incidents, names)
    n_influence = commit_influence(points, cursor_sql("company", company_last), player_points)

    membership = Changes()
    guild_sql = []   # a leaver's stance shares and declined invites go with the cursor move
    guild_last, guild_rows = process_membership(membership, incidents, names, guild_sql)
    commit(membership, people(membership.guids()), "\n".join([cursor_sql("guild", guild_last)] + guild_sql))

    chat = Changes()
    try:
        chat_last, n_lines, n_convs, n_moments = process_chat(chat, judge, meta)
        n_chat = commit(chat, people(chat.guids()), cursor_sql("chat", chat_last))
        words_incidents(chat, incidents, names)
    except JudgeUnreachable as e:
        n_lines = n_convs = n_moments = n_chat = 0
        log(f"judge unreachable, chat waits for the next cycle: {e}")

    talked = Changes()   # plan 18 P8c
    n_talk = bot_talk(talked, positions, judge, eras.get(args.era), meta, names)
    commit(talked, people(talked.guids()))
    words_incidents(talked, incidents, names)

    decay()
    decay_influence()
    reckoned = reckon(incidents, names)
    n_incidents = commit_incidents(incidents, args.era)
    described = 0 if args.no_words else describe(meta)
    shown = snapshot()
    stages = candidacy(chat, names)
    # Plan 18 P4: invites for offered candidacies and the hourly rank ladder, carried out by officer bots.
    if os.path.exists(ACTIONS_OFF):
        sql("UPDATE company_action SET done_at = NOW(), result = 'withdrawn' WHERE done_at IS NULL;", fetch=False)
        n_actions = "off"
    else:
        n_actions = create_actions(company_invites() + ladder() + company_life() + voiced, names, meta)
    founding = start_founding(args.era)   # plan 18 P5: runs in the background
    companies_snapshot()
    chat_lines = chat_snapshot()
    n_memories = memories_snapshot()
    n_journeys = journeys()
    company_words()
    log(f"lifecycle {born} new, {died} ended; presence {n_presence} moments; talk {n_talk}; events {event_rows} -> {n_events} changes; answers {answer_rows} -> {n_answers}; guild events {guild_rows}; "
        f"candidacy {'off' if stages is None else stages or 'none'}; company actions {n_actions}; "
        f"{'founding started for ' + str(founding) + '; ' if founding else ''}chat {n_lines} lines in {n_convs} conversations -> "
        f"{n_moments} moments, {n_chat} changes; {described} sentences written; snapshot {shown} people; "
        f"companies: {deeds} deeds in {n_influence} lands, {n_incidents} incidents; chat file {chat_lines} lines; "
        f"memories file {n_memories} rows"
        + (f"; journeys {n_journeys} rewritten" if n_journeys is not None else "")
        + (f", {reckoned} lands reckoned" if reckoned else ""))


def show(name):
    rows = sql("SELECT guid FROM characters WHERE name = " + q(name.capitalize()))
    if not rows:
        sys.exit(f"no character named {name}")
    guid = int(rows[0][0])
    print(f"How {name.capitalize()} feels:")
    for r in sql("SELECT o.name, r.score, r.familiarity, COALESCE(r.description, '') FROM regard r "
                 f"JOIN characters o ON o.guid = r.other_guid WHERE r.bot_guid = {guid} ORDER BY ABS(r.score) DESC LIMIT 20"):
        print(f"  {r[0]:14} {float(r[1]):6.1f} ({r[2]} moments) {words(float(r[1]))}. {chr(9).join(r[3:])}")
    print(f"How others feel about {name.capitalize()}:")
    for r in sql("SELECT b.name, r.score, r.familiarity, COALESCE(r.description, '') FROM regard r "
                 f"JOIN characters b ON b.guid = r.bot_guid WHERE r.other_guid = {guid} ORDER BY ABS(r.score) DESC LIMIT 20"):
        print(f"  {r[0]:14} {float(r[1]):6.1f} ({r[2]} moments) {words(float(r[1]))}. {chr(9).join(r[3:])}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "once"):
        p = sub.add_parser(name)
        p.add_argument("--era", default="classic")
        p.add_argument("--no-words", action="store_true", help="skip writing sentences (no writing-lane calls)")
        if name == "run":
            p.add_argument("--interval", type=int, default=120)
    s = sub.add_parser("show")
    s.add_argument("name")
    sub.add_parser("journeys")   # build the Journey files by hand, without a scoring cycle
    args = ap.parse_args()

    if args.cmd == "show":
        return show(args.name)
    if args.cmd == "journeys":
        t0 = time.time()
        written = journeys(force=True)
        log(f"journeys: {written} files rewritten in {time.time() - t0:.1f}s")
        return 0

    meta = eras.get(args.era)["meta"]
    # The judge's address lives in site/fleet.toml, not in three files that could disagree (23 W3).
    _j = fleet._fleet.judge()
    judge = fleet.Lane(_j["name"], _j["url"], _j["model"], JUDGE_SLOTS, set(), timeout=90,
                       lmstudio=_j["lmstudio"])
    while True:
        t0 = time.time()
        try:
            cycle(args, judge, meta)
        except Exception as e:  # the database or a backend went away: log it and try again next cycle
            log(f"cycle failed: {type(e).__name__}: {e}")
            if args.cmd == "once":
                return 1
        if args.cmd == "once":
            return 0
        time.sleep(max(10, args.interval - (time.time() - t0)))


if __name__ == "__main__":
    sys.exit(main())
