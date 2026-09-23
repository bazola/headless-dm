#!/usr/bin/env python3
"""Generate in-world guild histories and character backstories with the LLM fleet.

Standing rule: characters are people living in Azeroth, never game players.
Everything is written for one era (era.py), which is "now" for the people in it.

  generate  guild histories and member backstories as one pipeline: a guild's
            members are queued the moment its history is stored. --unguilded
            also queues bot characters outside any guild, behind the members,
            so slow lanes that can't take guild histories start on them at
            once. Only characters that exist in the era are written (Classic:
            no blood elves, draenei or death knights). Resumable: existing
            rows are skipped; rows from another era stop the run (reset first).
  sample    one backstory per lane for random unguilded bots, printed with
            timing and the judge's verdict. Nothing is stored.
  reset     dump the lore tables and mod-ollama-chat's personality tables into
            --backup-dir, then empty lore_guild and lore_character.
  project   copy lore into mod-ollama-chat (one manual-only personality
            template per character, assigned to it; stale BIO assignments are
            removed) and into guild.info / guild.motd. The worldserver caches
            guilds, so restart afterwards (`.ollama reload` also reloads the
            personalities).
  crafts    a trade and a household for each character (plan 33): the trade is
            read out of the professions they really hold in character_skills,
            so the claim is true, and the household is written against the
            story -- kin the backstory buried stay buried. Both land in
            lore_character; run `project` afterwards.
  restand   characters re-rolled to another level (level brackets) keep a story
            written for where they stood before. Evidence of where that was:
            the level a guild member's first level change after writing
            started from (ledger), or standing words the texts still carry
            from the prompt. Those stories, and characters moved two bands
            since they were fitted (standing_level), are revised to fit,
            names and events kept; the rest are stamped as fitting. Run
            `project` afterwards.

Fleet: jobs go straight to the backends through fleet.py lanes, so every model
works at once. Each output must pass the era's regex (META), then the judge
lane (qwen3-8b) for anachronisms a regex can't see. A text the judge still
flags on the last attempt is kept and written to review-<era>.jsonl.

Usage:
  python3 gen_backstories.py generate --era classic [--unguilded] [--lanes a,b] [--slots lane=n]
                                      [--limit N] [--no-judge]
  python3 gen_backstories.py sample --era classic [--lanes a,b]
  python3 gen_backstories.py sample-crafts --era classic [--per-lane 2]
  python3 gen_backstories.py crafts --era classic [--limit N] [--rewrite]
  python3 gen_backstories.py reset --backup-dir DIR
  python3 gen_backstories.py project
  python3 gen_backstories.py restand --era classic [--check-only] [--dry-run] [--limit N]
  python3 gen_backstories.py name-pools --era classic [--cultures Troll,Tauren]
  python3 gen_backstories.py repool --era classic --old OLD_NAMES_JSON --cultures Troll,Tauren [--dry-run]
"""
import argparse
import concurrent.futures as cf
import json
import os
import random
import re
import subprocess
import sys
import threading
import time

import era as eras
import fleet

LORE_DIR = os.path.dirname(os.path.abspath(__file__))
# Where this realm lives now comes from site/, with the live server config as the fallback (plan 23 W1).
# realpath, not abspath: the services run through /opt/wow symlinks, and abspath does not follow one --
# it would put /opt/wow on the path and `common` would not be there (plan 23 W1).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from common import site  # noqa: E402

RACES = {1: "Human", 2: "Orc", 3: "Dwarf", 4: "Night Elf", 5: "Forsaken", 6: "Tauren",
         7: "Gnome", 8: "Troll", 10: "Blood Elf", 11: "Draenei"}
HORDE = {2, 5, 6, 8, 10}
CLASSES = {1: "warrior", 2: "paladin", 3: "hunter", 4: "rogue", 5: "priest", 6: "death knight",
           7: "shaman", 8: "mage", 9: "warlock", 11: "druid"}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# database (mysql CLI; credentials from site/secrets.env, else the live conf. Never printed.)

_DB = site.db("characters")
_db_lock = threading.Lock()


def sql(query, fetch=True):
    host, port, user, pw, db = _DB
    # utf8mb4 explicitly: the CLI default is latin1, which double-encodes
    # anything non-ASCII on the way in.
    cmd = ["mysql", "--default-character-set=utf8mb4", "-h", host, "-P", port, "-u", user, db,
           "--batch", "--raw", "-N", "-e", query]
    with _db_lock:
        out = subprocess.run(cmd, env={"MYSQL_PWD": pw, "PATH": "/usr/bin:/bin"},
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
    s = s.translate(ASCII_PUNCT)
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def ensure_schema():
    sql(open(os.path.join(LORE_DIR, "lore_tables.sql")).read(), fetch=False)
    for table in ("lore_guild", "lore_character"):
        if not sql("SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() "
                   f"AND table_name = '{table}' AND column_name = 'era'"):
            # Rows written before eras existed were set during the war against the Lich King.
            sql(f"ALTER TABLE {table} ADD COLUMN era VARCHAR(16) NOT NULL DEFAULT 'wotlk' AFTER model",
                fetch=False)
    # Plan 15: personality and main motivation.
    have = {r[0] for r in sql("SELECT column_name FROM information_schema.columns WHERE table_schema = DATABASE() "
                              "AND table_name = 'lore_character'")}
    # Plan 33: the working life. LIFE_COLUMNS is defined far below, beside the code that writes it.
    for column, ddl in TRAIT_COLUMNS + LIFE_COLUMNS:
        if column not in have:
            sql(f"ALTER TABLE lore_character ADD COLUMN {column} {ddl}", fetch=False)
    # Plan 28: when a person wrote the text, their own words are kept beside the shaped prose, so it can be
    # reshaped later without asking them to write it again. CREATE TABLE IF NOT EXISTS never adds a column to a
    # table that is already there, so the live tables need this.
    for table in ("lore_character", "lore_alt"):
        if not sql("SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() "
                   f"AND table_name = '{table}' AND column_name = 'source'"):
            sql(f"ALTER TABLE {table} ADD COLUMN source TEXT NULL "
                f"COMMENT 'The player''s own words, when a person wrote this (plan 28)'", fetch=False)


def era_filter(e, alias="c"):
    return (f"{alias}.race IN ({','.join(map(str, sorted(e['races'])))}) "
            f"AND {alias}.class IN ({','.join(map(str, sorted(e['classes'])))})")


# ---------------------------------------------------------------------------
# prompts

def extract_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON object in reply: {text[:120]!r}")
    return json.loads(m.group(0))


GUILD_PROMPT = """You are a loremaster for {setting} Everything you write is real history to the people in it. Never mention games, players, levels as numbers, raids, loot, servers or anything outside the world.

Write the history of a {faction} company called "{name}". Treat the name as given: if it sounds odd or comic, explain in-world how the company came to be called that (a nickname that stuck, a founding joke, a mistranslation, a boast).

Its sworn members, with their rank in the company:
{roster}{others}

Ranks, highest first: {ranks}.

Reply with ONLY a JSON object, no markdown fences:
{{
  "history": "180 to 260 words: how and where the company was founded, by whom, what it fights for or profits from, one defining victory or loss, its reputation, and {guild_now}",
  "charter": "the company's public description as it would be posted in an inn or muster hall, under 450 characters",
  "motto": "the leaders' latest word to the company, under 110 characters",
  "member_roles": {{"<each member named above, exactly as given>": "one sentence on their place, duty or reputation in the company"}}
}}"""

# Members named in a company's history prompt. The reply carries a role line per name, and at 15 it already fits the
# 1400-token reply; a company of 50 (COMPANY_SIZE_MAX, regard.py) would cut the JSON off mid-object. The rest keep
# an empty role, as a recruit who joins after the history is written already does.
HISTORY_ROSTER = 15

CHAR_PROMPT = """You are a loremaster for {setting} Everything you write is real life to the person in it. Never mention games, players, levels as numbers, specs, raids, loot, servers or anything outside the world.{limits}

Write the backstory of {name}, a {gender} {race} {cls} of the {faction}, {standing}.
Temperament: {temperament}
{guild_block}
{keep_block}Write 170 to 210 words in the second person, speaking to them as "you" and "your". Open somewhere that belongs to this person alone, and vary the shape of the first sentence: it must not always be "You" followed by a verb, and the opening must not always be a scar, a habit, a kept object or a remembered smell. Never begin with "You were born", "You still", "You return", "You keep", "You trace", "Your left", "Your fingers", "Your knuckles" or "The scar". Cover: where you come from and your family; what set you on the path of a {cls}; one event in the wars of recent years that marked you; {guild_reason}; one thing you want and one thing you fear; one thing you love in life; and one good memory from before the wars. {relation_line} End with one sentence on how you speak. Keep it true to {race} culture and to the history of Azeroth. Don't use the names {overused}; give invented people names of their own. Plain prose only: no title, no lists, no quotes around it."""


def temperaments():
    rows = sql("SELECT `key`, prompt FROM mod_ollama_chat_personality_templates "
               "WHERE manual_only = 0")
    if not rows:
        sys.exit("no temperament templates")
    return rows


def roster(guildid, e):
    return [dict(guid=int(r[0]), name=r[1], race=int(r[2]), cls=int(r[3]), gender=int(r[4]),
                 level=int(r[5]), rank=int(r[6]), rname=r[7])
            for r in sql(
                "SELECT c.guid, c.name, c.race, c.class, c.gender, c.level, m.rank, gr.rname "
                "FROM guild_member m JOIN characters c ON c.guid = m.guid "
                "JOIN guild_rank gr ON gr.guildid = m.guildid AND gr.rid = m.rank "
                f"WHERE m.guildid = {guildid} AND {era_filter(e)} ORDER BY m.rank, c.name")]


def history_roster(members, cap=HISTORY_ROSTER):
    """The leader and officers first (rank order), then a random draw of the rest, shown in rank order."""
    if len(members) <= cap:
        return members
    ranked = sorted(members, key=lambda m: m["rank"])
    top = [m for m in ranked if m["rank"] <= 1][:cap]
    rest = [m for m in ranked if m not in top]
    picked = top + random.sample(rest, cap - len(top))
    return sorted(picked, key=lambda m: (m["rank"], m["name"]))


def describe(m, e):
    return (f"- {m['name']}: {'female' if m['gender'] else 'male'} {RACES.get(m['race'], 'unknown')} "
            f"{CLASSES.get(m['cls'], 'adventurer')}, {eras.standing(m['level'], e)}; rank {m['rname']}")


def char_prompt(c, temper, guild, e, bond=None, main_name=None, concept=None, main_sheet=None, keep=()):
    faction = "Horde" if c["race"] in HORDE else "Alliance"
    race, cls = RACES.get(c["race"], "unknown"), CLASSES.get(c["cls"], "adventurer")
    if guild:
        mates = [m for m in guild["members"] if m["guid"] != c["guid"]]
        picks = random.sample(mates, min(3, len(mates)))
        role = guild["roles"].get(c["name"], "")
        guild_block = (f"\nYou are sworn to the company \"{guild['name']}\" with the rank {c['rname']}. "
                       f"{role}\nThe company's history:\n{guild['history']}\n"
                       f"Some of your fellow members:\n" + "\n".join(describe(m, e) for m in picks) + "\n")
        guild_reason = "why you joined the company and what it means to you"
        relation_line = ("Name one of the fellow members above and say what is between you." if picks else
                         "Name one person (invented, not famous) who matters to you.")
    else:
        guild_block = ""
        guild_reason = "why you travel alone, sworn to no company"
        relation_line = "Name one person (invented, not famous) who matters to you."
    if concept:
        # The player's own words about this character. They reached the bond prompt but not this one, so any
        # trait the bond happened not to echo was lost: Grommell's wealth vanished exactly this way.
        guild_block += (f"\nWhat the player says is true of {c['name']}, every word of which must survive in "
                        f"what you write:\n{concept.strip()}\n")
    if bond:
        if main_sheet:
            # Without this the story cannot know the main's own history and quietly contradicts it: one run
            # killed off the uncle who is alive and a noble of Ironforge in the sheet.
            guild_block += (f"\nWhat is known of {main_name}, none of which you may contradict, and to whose "
                            f"people, places and losses you must keep:\n{main_sheet.strip()}\n")
        # Plan 21 §4: an alt's story is built around the bond, so the bond replaces the invented relation.
        guild_block += f"\nThe one who matters most to you is {main_name}. What is between you:\n{bond}\n"
        relation_line = (f"Say what is between you and {main_name}, as it is written above. Invent no deeds for "
                         f"{main_name}, and give them no words or feelings that are not there.")
    # Ask for the words the check demands, exactly as the bond prompt does. Without this the story check is
    # guesswork: six straight attempts failed on "wealth" while nothing in the prompt ever requested it.
    keep_block = ("These exact words must appear in what you write: " + ", ".join(keep) + ".\n\n") if keep else ""
    return CHAR_PROMPT.format(keep_block=keep_block, setting=e["setting"], limits=e["limits"], name=c["name"],
                              gender="female" if c["gender"] else "male", race=race, cls=cls,
                              faction=faction, standing=eras.standing(c["level"], e),
                              temperament=temper[1], guild_block=guild_block,
                              guild_reason=guild_reason, relation_line=relation_line,
                              overused=", ".join(OVERUSED_NAMES))


# ---------------------------------------------------------------------------
# vetting

_review_lock = threading.Lock()


def vet(text, e, judge, attempt, label):
    """Raise to retry; on the last attempt a judge flag is recorded for review instead."""
    bad = e["meta"].search(text)
    if bad:
        raise ValueError(f"out-of-world term: {bad.group(0)!r}")
    if not judge:
        return
    evidence = judge.check(eras.judge_prompt(e, text))
    if not evidence:
        return
    if attempt < fleet.MAX_ATTEMPTS:
        raise ValueError(f"judge: {evidence}")
    with _review_lock, open(os.path.join(LORE_DIR, f"review-{e['name']}.jsonl"), "a") as f:
        f.write(json.dumps({"label": label, "evidence": evidence, "text": text}) + "\n")


def check_backstory(text, words_range=(70, 300)):
    text = re.sub(r"\s+", " ", text).strip().strip('"')
    words = len(text.split())
    if not words_range[0] <= words <= words_range[1]:
        raise ValueError(f"{words} words")
    # Second person, but no mandated opening word: requiring "You..." here is what drove 99.8%
    # of backstories to begin "You were born", and the gist summarises the backstory, so the
    # sameness was inherited by a field that reaches every prompt. The two checks are reported
    # apart because one message for both made a first-person leak read as a missing "you".
    first = FIRST_PERSON.search(text)
    if first:
        raise ValueError(f"slips into first person ({first.group(0)!r}): {text[:60]!r}")
    if not SECOND_PERSON.search(text):
        raise ValueError(f"not second person: {text[:60]!r}")
    if STOCK_BACKSTORY_RE.match(text):
        raise ValueError(f"stock opening: {text[:60]!r}")
    name = OVERUSED_RE.search(text)
    if name:
        raise ValueError(f"overused name {name.group(0)!r}")
    return text


# ---------------------------------------------------------------------------
# jobs

def gen_guild(lane, attempt, e, judge, guildid, name):
    members = roster(guildid, e)
    if not members:
        return None
    faction = "Horde" if members[0]["race"] in HORDE else "Alliance"
    ranks = [r[0] for r in sql(f"SELECT rname FROM guild_rank WHERE guildid = {guildid} ORDER BY rid")]
    named = history_roster(members)
    others = len(members) - len(named)
    prompt = GUILD_PROMPT.format(setting=e["setting"], faction=faction, name=name,
                                 roster="\n".join(describe(m, e) for m in named),
                                 others=(f"\n...and {others} more sworn members, not named here."
                                         if others else ""),
                                 ranks=", ".join(ranks), guild_now=e["guild_now"])
    data = extract_json(lane.chat([{"role": "user", "content": prompt}], 1400, temperature=0.85))
    history, charter, motto = data["history"].strip(), data["charter"].strip(), data["motto"].strip()
    names = {m["name"] for m in named}
    roles = {k: v for k, v in data.get("member_roles", {}).items() if isinstance(v, str) and k in names}
    if len(history.split()) < 120:
        raise ValueError(f"history too short ({len(history.split())} words)")
    vet(" ".join([history, charter, motto, *roles.values()]), e, judge, attempt, f"guild {guildid} {name}")
    sql("INSERT INTO lore_guild (guildid, history, charter, motto, member_roles, model, era) VALUES "
        f"({guildid}, {q(history)}, {q(charter[:500])}, {q(motto[:128])}, {q(json.dumps(roles))}, "
        f"{q((lane.name + ':' + lane.model)[:64])}, {q(e['name'])})", fetch=False)
    return dict(guildid=guildid, name=name, history=history, roles=roles, members=members)


def gen_character(lane, attempt, e, judge, c, temper, guild):
    text = check_backstory(lane.chat([{"role": "user", "content": char_prompt(c, temper, guild, e)}], 500))
    vet(text, e, judge, attempt, f"character {c['guid']} {c['name']}")
    gid = str(guild["guildid"]) if guild else "NULL"
    sql("INSERT INTO lore_character (guid, guildid, temperament, backstory, model, era) VALUES "
        f"({c['guid']}, {gid}, {q(temper[0])}, {q(text)}, {q((lane.name + ':' + lane.model)[:64])}, "
        f"{q(e['name'])})", fetch=False)


# ---------------------------------------------------------------------------
# overused invented names (plan 15 diversity pass)

# The backstory run reused a few invented names across hundreds of unrelated characters
# (Elara in 251 backstories, Kaelen in 125). Value: the gender of the person they name.
OVERUSED_NAMES = {"Elara": "female", "Elira": "female", "Lyra": "female", "Mira": "female",
                  "Kaelen": "male", "Thorne": "male"}
OVERUSED_RE = re.compile(r"\b(" + "|".join(OVERUSED_NAMES) + r")\b")
NAMES_FILE = os.path.join(LORE_DIR, "names.json")
# Whose naming customs an invented person follows: the character's own people (the Forsaken were human).
NAME_CULTURE = {1: "Human", 2: "Orc", 3: "Dwarf", 4: "Night Elf", 5: "Human", 6: "Tauren", 7: "Gnome", 8: "Troll",
                10: "Blood Elf", 11: "Draenei"}
FAMOUS = {"thrall", "jaina", "arthas", "sylvanas", "cairne", "vol'jin", "magni", "varian", "bolvar", "anduin",
          "tyrande", "malfurion", "illidan", "rexxar", "garrosh", "uther", "antonidas", "medivh", "khadgar",
          "turalyon", "alleria", "grom", "durotan", "orgrim", "ner'zhul", "gul'dan", "baine", "hamuul", "rokhan",
          "zul'jin", "sen'jin", "muradin", "brann", "gelbin", "mekkatorque", "fandral", "shandris", "nathanos",
          "varimathras", "rend", "nefarian", "onyxia", "ragnaros", "hakkar", "kazzak", "azuregos", "vancleef"}

# How a people's names sound, where the models otherwise drift: the first troll pools read orcish, the tauren pools
# orcish or elvish. The names given as the manner are not offered back (NAME_STYLE_EXAMPLES).
NAME_STYLE = {
    "Troll": ("Darkspear trolls give their children rolling, sing-song names of two or three syllables with open vowels "
              "(often doubled) and the sounds j, z, k, n and r, in the manner of Sen'jin, Zen'tabra, Ula'elek, "
              "Kin'weelay, Jhash, Zalazane or Mai'ah. About half carry an apostrophe between syllables and half none. "
              "Vary the first letter and syllable from name to name. Not orcish: no harsh clusters such as Grom, Drak, "
              "Brog, Thrak, Gorg or Krag."),
    "Tauren": ("Tauren given names come from Taur-ahe, the tauren tongue: short, earthy words of one to three "
               "syllables built from soft, breathing sounds (h, k, m, n, t and w, with some s, l, r and b) and broad "
               "vowels (a, o, u and ee), in the manner of Hurn, Una, Mahnott, Kawnie, Tagain, Ahanu, Nata, Moorat, Tepa "
               "or Hewa. Vary the first letter from name to name. Not orcish (no Grom, Drak, Gor, Zul, Thar, -nok or "
               "-gath), not elvish (no Syl-, Kael-, -ith or -ara), and not the names of dwarves or humans (no Borin, "
               "Brenna, Galen, Doran, Daelin, Mara or Kira)."),
}
NAME_STYLE_EXAMPLES = {"sen'jin", "zen'tabra", "ula'elek", "kin'weelay", "jhash", "zalazane", "mai'ah", "hurn", "una",
                       "mahnott", "kawnie", "tagain", "ahanu", "nata", "moorat", "grunth", "sunn", "tepa"}
# Checked after the model answers, which doesn't always follow NAME_STYLE.
NAME_BANNED = {
    "Troll": re.compile(r"grom|drak|brog|thrak|gorg|krag", re.I),
    "Tauren": re.compile(r"grom|drak|zul|thar|gath|gor|nok$|ruk$|^syl|^kael|ith$|ara$|"
                         r"^(galen|doran|daelin|borin|brenna|brynn|cora|ella|jenna|lira|mara|kira|kaya|kyla|lyna|nyla|mira|terra|ursa)$",
                         re.I),
}

NAME_POOL_PROMPT = """List {n} {gender} given names that a {culture} of Azeroth might carry, true to how the {culture} people name their children: varied sounds, lengths and first letters.{style} No famous figures from the world's history, and none of: {avoid}. Reply with ONLY a JSON object: {{"names": ["...", "..."]}}"""


def name_pools(args):
    e = eras.get(args.era)
    taken = {r[0].lower() for r in sql("SELECT name FROM characters")}
    avoid = FAMOUS | NAME_STYLE_EXAMPLES | {n.lower() for n in OVERUSED_NAMES} | {t.lower() for t, _, _ in e["targets"]}
    lane = fleet.pick_lanes("evo-quality")[0]
    cultures = sorted({NAME_CULTURE[r] for r in e["races"]})
    only = {c.strip() for c in args.cultures.split(",") if c.strip()}
    if only:
        cultures = [c for c in cultures if c in only]
    # Named cultures are rewritten; every other pool in the file is kept.
    pools = json.load(open(NAMES_FILE)) if only and os.path.exists(NAMES_FILE) else {}
    for key in [k for k in pools if k.split("|")[0] in cultures]:
        del pools[key]
    for culture in cultures:
        for gender in ("female", "male"):
            names, tries = set(), 0
            banned = NAME_BANNED.get(culture)
            # A name belongs to one pool of any people, no letter opens more than a sixth of a pool, and apostrophes
            # stay under three names in five (the models otherwise give every troll one).
            other = {m for listed in pools.values() for m in listed}
            letter_cap = max(3, args.per_pool // 6)
            while len(names) < args.per_pool and tries < 16:
                tries += 1
                prompt = NAME_POOL_PROMPT.format(n=args.per_pool, gender=gender, culture=culture,
                                                 style=" " + NAME_STYLE[culture] if culture in NAME_STYLE else "",
                                                 avoid=", ".join(OVERUSED_NAMES))
                try:
                    listed = extract_json(lane.chat([{"role": "user", "content": prompt}], 1200, temperature=1.0))["names"]
                except (ValueError, KeyError, RuntimeError) as ex:
                    log(f"  retry {culture} {gender}: {ex}")
                    continue
                for n in listed:
                    n = str(n).strip().translate(ASCII_PUNCT)
                    # At most two names to an opening: left alone, the models list one sound with many endings.
                    opening = n.lower().split("'")[0][:3]
                    if (re.fullmatch(r"[A-Z][a-z']{2,11}", n) and n.lower() not in taken and n.lower() not in avoid
                            and not e["meta"].search(n) and not (banned and banned.search(n))
                            and sum(1 for m in names if m.lower().split("'")[0][:3] == opening) < 2
                            and n not in other and sum(1 for m in names if m[0] == n[0]) < letter_cap
                            and ("'" not in n or sum(1 for m in names if "'" in m) < args.per_pool * 0.6)):
                        names.add(n)
            pools[f"{culture}|{gender}"] = sorted(names)
            log(f"{culture} {gender}: {len(pools[f'{culture}|{gender}'])} names")
    with open(NAMES_FILE, "w") as f:
        json.dump(pools, f, indent=1, ensure_ascii=False)
    log(f"wrote {NAMES_FILE}")
    return 0


def rename_overused(args):
    e = eras.get(args.era)
    pools = json.load(open(NAMES_FILE))
    fields = ("backstory", "gist", "motivation", "motivation_short")
    rows = sql("SELECT l.guid, c.name, c.race, "
               + ", ".join(_flat(f"l.{f}") for f in fields) + ", "
               f"{_flat(TARGET_NAME_SQL)}, {_flat('lg.history')} "
               "FROM lore_character l JOIN characters c ON c.guid = l.guid "
               "LEFT JOIN guild_member m ON m.guid = l.guid LEFT JOIN lore_guild lg ON lg.guildid = m.guildid "
               f"WHERE l.era = {q(e['name'])} AND CONCAT_WS(' ', l.backstory, l.gist, l.motivation, l.motivation_short) "
               f"REGEXP {q(chr(92) + 'b(' + '|'.join(OVERUSED_NAMES) + ')' + chr(92) + 'b')}")
    used = {}
    rng = random.Random(args.seed)
    changed = shared_kept = 0
    for r in rows:
        guid, name, race = int(r[0]), r[1], int(r[2])
        texts = dict(zip(fields, r[3:7]))
        target, history = r[7], r[8]
        everything = " ".join(texts.values()) + " " + target
        found = sorted(set(OVERUSED_RE.findall(everything)))
        mapping = {}
        for old in found:
            if re.search(rf"\b{old}\b", history):
                shared_kept += 1        # a figure of the guild's own history: every member knows the same person
                continue
            pool = pools.get(f"{NAME_CULTURE.get(race, 'Human')}|{OVERUSED_NAMES[old]}", [])
            choices = [n for n in pool if n not in mapping.values() and n != name
                       and not re.search(rf"\b{re.escape(n)}\b", everything)]
            if not choices:
                continue
            pick = min(choices, key=lambda n: (used.get(n, 0), rng.random()))
            used[pick] = used.get(pick, 0) + 1
            mapping[old] = pick
        if not mapping:
            continue
        swap = lambda text: re.sub(r"\b(" + "|".join(mapping) + r")\b", lambda m: mapping[m.group(1)], text)
        new = {f: swap(t) for f, t in texts.items()}
        new_target = swap(target)
        changed += 1
        if args.dry_run:
            if changed <= args.show:
                print(f"== {name}: {', '.join(f'{a} -> {b}' for a, b in mapping.items())}\n   {new['motivation_short']}")
            continue
        sets = ", ".join(f"{f} = {q(new[f])}" for f in fields if new[f] != texts[f])
        if new_target != target:
            sets += f", motivation_target = JSON_SET(motivation_target, '$.name', {q(new_target)})"
        sql(f"UPDATE lore_character SET {sets.lstrip(', ')} WHERE guid = {guid}", fetch=False)
    left = sql(f"SELECT COUNT(*) FROM lore_character WHERE era = {q(e['name'])} AND CONCAT_WS(' ', backstory, gist, motivation, "
               f"motivation_short) REGEXP {q(chr(92) + 'b(' + '|'.join(OVERUSED_NAMES) + ')' + chr(92) + 'b')}")[0][0]
    most = sorted(used.items(), key=lambda kv: -kv[1])[:5]
    log(f"{'would rename' if args.dry_run else 'renamed'} in {changed} characters; {shared_kept} guild-history figures kept; "
        f"{left} characters still mention an overused name; most used new names: {most}")
    return 0


def repool(args):
    """Rename invented people whose names came from a replaced name pool, in characters of that culture, to names from
    the current pools. Same rules as rename-overused: one character at a time, the same person renamed the same way
    across its texts, and a figure of its guild's history kept (every member knows that person)."""
    e = eras.get(args.era)
    old_pools, pools = json.load(open(args.old)), json.load(open(NAMES_FILE))
    taken = {r[0] for r in sql("SELECT name FROM characters")}
    fields = ("backstory", "gist", "motivation", "motivation_short")
    rng = random.Random(args.seed)
    used, changed, shared_kept = {}, 0, 0
    for culture in [c.strip() for c in args.cultures.split(",") if c.strip()]:
        fresh = {n for key, names in pools.items() if key.startswith(culture + "|") for n in names}
        old = {n: key.split("|")[1] for key, names in old_pools.items() if key.startswith(culture + "|")
               for n in names if n not in fresh and n not in taken}
        races = sorted(r for r, c in NAME_CULTURE.items() if c == culture and r in e["races"])
        if not old or not races:
            continue
        old_re = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(old, key=len, reverse=True)) + r")\b")
        rows = sql("SELECT l.guid, c.name, " + ", ".join(_flat(f"l.{f}") for f in fields) + ", "
                   f"{_flat(TARGET_NAME_SQL)}, {_flat('lg.history')} "
                   "FROM lore_character l JOIN characters c ON c.guid = l.guid "
                   "LEFT JOIN guild_member m ON m.guid = l.guid LEFT JOIN lore_guild lg ON lg.guildid = m.guildid "
                   f"WHERE l.era = {q(e['name'])} AND c.race IN ({','.join(map(str, races))})")
        for r in rows:
            guid, name = int(r[0]), r[1]
            texts = dict(zip(fields, r[2:6]))
            target, history = r[6], "\t".join(r[7:])
            everything = " ".join(texts.values()) + " " + target
            mapping = {}
            for found in sorted(set(old_re.findall(everything))):
                if re.search(rf"\b{re.escape(found)}\b", history):
                    shared_kept += 1
                    continue
                choices = [n for n in pools.get(f"{culture}|{old[found]}", []) if n not in mapping.values()
                           and n != name and not re.search(rf"\b{re.escape(n)}\b", everything)]
                if not choices:
                    continue
                pick = min(choices, key=lambda n: (used.get(n, 0), rng.random()))
                used[pick] = used.get(pick, 0) + 1
                mapping[found] = pick
            if not mapping:
                continue
            swap_re = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(mapping, key=len, reverse=True)) + r")\b")
            swap = lambda text: swap_re.sub(lambda m: mapping[m.group(1)], text)
            new = {f: swap(t) for f, t in texts.items()}
            changed += 1
            if args.dry_run:
                if changed <= args.show:
                    pairs = ", ".join(f"{a} -> {b}" for a, b in mapping.items())
                    print(f"== {name}: {pairs}\n   {new['gist']}")
                continue
            sets = ", ".join(f"{f} = {q(new[f])}" for f in fields if new[f] != texts[f])
            if swap(target) != target:
                sets += f", motivation_target = JSON_SET(motivation_target, '$.name', {q(swap(target))})"
            if sets:
                sql(f"UPDATE lore_character SET {sets.lstrip(', ')} WHERE guid = {guid}", fetch=False)
    most = sorted(used.items(), key=lambda kv: -kv[1])[:5]
    log(f"{'would rename' if args.dry_run else 'renamed'} in {changed} characters; {shared_kept} guild-history figures "
        f"kept; most used new names: {most}")
    return 0


# ---------------------------------------------------------------------------
# traits: personality and main motivation (plan 15)

TRAIT_COLUMNS = [
    ("standing_level", "TINYINT UNSIGNED NULL COMMENT 'The level its texts were last fitted to (traits, restand)'"),
    ("personality", "VARCHAR(400) NULL COMMENT 'Who they are in any room; every prompt'"),
    ("gist", "VARCHAR(600) NULL COMMENT 'The backstory in about 45 words; every prompt'"),
    ("motivation", "TEXT NULL COMMENT 'What they live for, 40-70 words; conversations'"),
    ("motivation_short", "VARCHAR(255) NULL COMMENT 'The motivation in one line; every prompt'"),
    ("motivation_kind", "VARCHAR(16) NULL"),
    ("motivation_target", "JSON NULL COMMENT '{type: foe|person|place|faction|cause, name}'"),
]
TARGET_TYPES = {"foe", "person", "place", "faction", "cause"}

TRAITS_PROMPT = """You are a loremaster for {setting} Everything you write is real life to the person in it. Never mention games, players, levels as numbers, specs, raids, loot, servers or anything outside the world.{limits}

This is {name}, a {gender} {race} {cls} of the {faction}, {standing}.{guild_line}
Temperament: {temperament}
Their story, as told to them:
{backstory}

Write four things about {name}, true to the story above and to {race} culture. Every one speaks to {name} as "you" and "your": never "I", "me" or "my", and never a bare fragment. Keep the names your story uses; never introduce the names {overused}.
1. "personality": who you are in any room, at most 35 words and never more than 60: two or three traits, how you carry yourself, how you talk, one telling habit, and the one thing you love from the story above - named, or shown in what you notice and reach for. Specific to you, not a type. Don't begin it with "You move like" or "You carry yourself".
2. "gist": your story in at most 45 words, keeping its names and the one event that marked you. Do not begin it with "Born", "Raised" or "You were born", and do not open on a birthplace: start from whatever in the story weighs most on you now.
3. "motivation": what you are living for, 40 to 70 words, built on what your story says you want (don't open with "You live for"). Its kind must be one of these three: {kinds}. Say the goal, why it matters to you, what you have already done toward it, and what you would risk. It may be far beyond you today.
4. "motivation_short": the same motivation in one sentence of at most 20 words. Lead with the thing itself - the goal, the place, the debt, the person - rather than with the word "You": never begin with "You seek", "You strive", "You mean", "You aim", "You hunt" or "You want". The sentence must still contain "you" or "your" somewhere after that opening, exactly as the example below does ("Every copper you earn goes to..."). Move the pronoun out of the opening; do not drop it.
Also give "motivation_kind" (exactly one of: {kinds}) and "motivation_target": what the motivation is aimed at, as {{"type": "foe" or "person" or "place" or "faction" or "cause", "name": "..."}}. A foe or faction must be one of these, or someone named in your story: {targets}. A person, place or cause can be anyone or anything from your story; give it a proper name wherever the story has one.

The voice wanted, written for someone else (reuse none of it, including the words it opens on): {{"personality": "You are blunt and patient, keep your back to walls, and answer questions with questions. You whistle through your teeth when you lie.", "motivation_short": "Every copper you earn goes to buying back the farm the Hillard brothers burned."}}

Reply with ONLY a JSON object with the keys personality, gist, motivation, motivation_short, motivation_kind, motivation_target. No markdown fences."""


def _flat(col):
    # mysql --batch --raw prints newlines and tabs raw; keep one row per line.
    return f"REPLACE(REPLACE(COALESCE({col}, ''), CHAR(10), ' '), CHAR(9), ' ')"


DASHBOARD_LORE = os.path.join(site.get("DATA_DIR", "/opt/wow/server/data"), "dashboard-data", "lore.json")
TARGET_NAME_SQL = """JSON_UNQUOTE(JSON_EXTRACT(l.motivation_target, '$.name'))"""
ROLE_SQL = """JSON_UNQUOTE(JSON_EXTRACT(lg.member_roles, CONCAT('$."', c.name, '"')))"""
HISTORY_SQL = """SUBSTRING_INDEX(lg.history, ' ', 60)"""


def trait_rows(e, where="l.personality IS NULL", extra=""):
    rows = sql("SELECT l.guid, c.name, c.race, c.class, c.gender, c.level, "
               f"{_flat('t.prompt')}, {_flat('l.backstory')}, {_flat('g.name')}, {_flat('gr.rname')}, "
               f"{_flat(ROLE_SQL)}, {_flat(HISTORY_SQL)} "
               "FROM lore_character l JOIN characters c ON c.guid = l.guid "
               "LEFT JOIN mod_ollama_chat_personality_templates t ON t.`key` = l.temperament "
               "LEFT JOIN guild_member m ON m.guid = l.guid LEFT JOIN guild g ON g.guildid = m.guildid "
               "LEFT JOIN guild_rank gr ON gr.guildid = m.guildid AND gr.rid = m.rank "
               "LEFT JOIN lore_guild lg ON lg.guildid = m.guildid "
               f"WHERE l.era = {q(e['name'])} AND {where} AND {era_filter(e)} {extra}")
    return [dict(guid=int(r[0]), name=r[1], race=int(r[2]), cls=int(r[3]), gender=int(r[4]), level=int(r[5]),
                 temperament=r[6], backstory=r[7], guild=r[8], rank=r[9], role=r[10], history=r[11]) for r in rows]


# Caps re-tuned after the longer backstory (plan 37 §12) inflated every field that summarises it.
# Measured over 11 generations against a 250-word median story: personality 31-72 (was capped 50, then 58,
# then 65 - each raise chased the model), gist 34-67 against a 60 cap where the 90-generation baseline
# overflowed only 2.2%, motivation_short 12-27 against 26 where the baseline was 1.1%.
#
# Raising caps alone is whack-a-mole, and personality has a hard wall: varchar(400) at ~5.4 chars/word is
# about 70 words, so it CANNOT be raised much further without widening TRAIT_WIDTH and the column. Hence
# the ask was tightened (40 -> 35, with an explicit "never more than 60") and only the caps with column
# room were widened: gist to 70 (varchar 600) and motivation_short to 30 (varchar 255).
TRAIT_WORDS = (("personality", 8, 70), ("gist", 15, 70), ("motivation", 35, 100), ("motivation_short", 5, 30))
TRAIT_WIDTH = {"personality": 400, "gist": 600, "motivation_short": 255}

# Not "mine": a mine is a place ("you seek a mine untouched by war").
FIRST_PERSON = re.compile(r"\bI\b|\b(?i:me|my|myself)\b")
SECOND_PERSON = re.compile(r"\byou(?:r|rs|rself)?\b", re.I)

# Stock openings for the two fields that reach every prompt. Both were once *required* to
# open a fixed way -- motivation_short by a "starts with You" check, gist by inheriting the
# backstory's mandated "You were born" -- which left 1,269 characters sharing 47 distinct
# two-word motivation openings and 90% of gists beginning "Born". Second person is still
# required (the TRAIT_WORDS loop checks it); only the fixed first words are not.
STOCK_MOTIVATION_RE = re.compile(
    r"^\s*you\s+(seek|strive|mean|aim|hunt|want|wish|long|intend|live|search|hope|swear|carry)\b", re.I)
STOCK_GIST_RE = re.compile(r"^\s*(born\b|you\s+were\s+born\b|raised\s+in\b)", re.I)
# Removing the mandated "You were born" did not end the sameness, it moved it: the replacement
# prompt offered "a habit, a scar, a debt, a place they keep returning to" and the models read
# that list as the new template -- 52% of a fresh run opened "You still", "You return" or
# "Your left". A suggestion list in a prompt becomes a template unless it is fenced like this.
# Only the openings actually measured as dominant. Adding guesses here backfired: blocking
# "wake" and "sleep" simply moved the models to "You woke" (28 of 460), bought nothing, and
# cost 49 retries and 3 characters that exhausted every attempt. A blocklist invites inflection,
# so it stays short and evidence-based; emergent clusters ("You sit", "The wind") are the
# sweep's job, because vary-traits' shared opener counter is generic and needs no new pattern.
STOCK_BACKSTORY_RE = re.compile(
    r"^\s*(you\s+(were\s+born|still|return|keep|trace)\b"
    r"|your\s+(left|right|fingers|knuckles)\b"
    r"|the\s+scar\b)", re.I)


def _words(text):
    return len(text.split())


def build_traits(lane, c, e, judge, rng):
    """One call. Returns (data, kinds, problems, judge_flag); nothing is stored here."""
    kinds = eras.draw_kinds(c["race"], c["cls"], c["backstory"], rng)
    guild_line = ""
    if c["guild"]:
        guild_line = f" Sworn to the company \"{c['guild']}\" with the rank {c['rank']}. {c['role']}"
        if c["history"]:
            guild_line += f" The company's story begins: {c['history']}..."
    prompt = TRAITS_PROMPT.format(
        setting=e["setting"], limits=e["limits"], name=c["name"], gender="female" if c["gender"] else "male",
        race=RACES.get(c["race"], "unknown"), cls=CLASSES.get(c["cls"], "wanderer"),
        faction="Horde" if c["race"] in HORDE else "Alliance", standing=eras.standing(c["level"], e),
        guild_line=guild_line, temperament=c["temperament"], backstory=c["backstory"],
        kinds=", ".join(kinds), targets=", ".join(t for t, _, _ in e["targets"]), overused=", ".join(OVERUSED_NAMES))
    raw = extract_json(lane.chat([{"role": "user", "content": prompt}], 900, temperature=0.85))
    # Fold punctuation first: q() would otherwise lengthen the text after the column-size cut.
    data = {k: re.sub(r"\s+", " ", str(raw.get(k) or "").translate(ASCII_PUNCT)).strip().strip('"')
            for k in ("personality", "gist", "motivation", "motivation_short", "motivation_kind")}
    target = raw.get("motivation_target") if isinstance(raw.get("motivation_target"), dict) else {}
    data["motivation_target"] = {"type": str(target.get("type", "")).strip().lower(),
                                 "name": re.sub(r"\s+", " ", str(target.get("name", ""))).strip()}
    data["motivation_kind"] = data["motivation_kind"].lower()

    problems = []
    for key, low, high in TRAIT_WORDS:
        n = _words(data[key])
        first = FIRST_PERSON.search(data[key])
        if not low <= n <= high:
            problems.append(f"{key} has {n} words")
        elif first:
            problems.append(f"{key} slips into first person ({first.group(0)!r}): {data[key][:40]!r}")
        elif not SECOND_PERSON.search(data[key]):
            problems.append(f"{key} never says you: {data[key][:40]!r}")
    # Second person is already required for every field by the TRAIT_WORDS loop above; these
    # two only reject the stock first words that made the two every-prompt fields interchangeable.
    if data["motivation_short"] and STOCK_MOTIVATION_RE.match(data["motivation_short"]):
        problems.append(f"motivation_short uses a stock opening: {data['motivation_short'][:40]!r}")
    if data["gist"] and STOCK_GIST_RE.match(data["gist"]):
        problems.append(f"gist uses a stock opening: {data['gist'][:40]!r}")
    if data["motivation_kind"] not in kinds:
        problems.append(f"kind {data['motivation_kind']!r} not offered ({', '.join(kinds)})")
    t = data["motivation_target"]
    if t["type"] not in TARGET_TYPES or not t["name"]:
        problems.append(f"bad target {t}")
    elif t["type"] in ("foe", "faction") and not eras.target_known(e, t["name"], c["backstory"]):
        problems.append(f"unknown {t['type']} {t['name']!r}")
    text = " ".join([data["personality"], data["gist"], data["motivation"], data["motivation_short"], t["name"]])
    introduced = {n for n in OVERUSED_RE.findall(text) if n not in c["backstory"]}
    if introduced:
        problems.append(f"introduces overused name {sorted(introduced)[0]!r}")
    bad = e["meta"].search(text)
    if bad:
        problems.append(f"out-of-world term: {bad.group(0)!r}")
    flag = None if problems or not judge else judge.check(eras.judge_prompt(e, text))
    return data, kinds, problems, flag


def gen_traits(lane, attempt, e, judge, c, rng):
    data, kinds, problems, flag = build_traits(lane, c, e, judge, rng)
    if problems:
        raise ValueError(problems[0])
    if flag:
        if attempt < fleet.MAX_ATTEMPTS:
            raise ValueError(f"judge: {flag}")
        with _review_lock, open(os.path.join(LORE_DIR, f"review-{e['name']}.jsonl"), "a") as f:
            f.write(json.dumps({"label": f"traits {c['guid']} {c['name']}", "evidence": flag, "text": data}) + "\n")
    sql(f"UPDATE lore_character SET personality = {q(data['personality'][:400])}, gist = {q(data['gist'][:600])}, "
        f"motivation = {q(data['motivation'])}, motivation_short = {q(data['motivation_short'][:255])}, "
        f"motivation_kind = {q(data['motivation_kind'])}, motivation_target = {q(json.dumps(data['motivation_target']))}, "
        f"standing_level = {c['level']} "
        f"WHERE guid = {c['guid']}", fetch=False)
    return f"{data['motivation_kind']} -> {data['motivation_target']['name']}"


def traits(args):
    e = eras.get(args.era)
    ensure_schema()
    lanes = [l for l in fleet.pick_lanes(args.lanes, args.slots) if "traits" in l.kinds]
    judge = None if args.no_judge else fleet.Judge()
    rows = trait_rows(e, extra=f"ORDER BY l.guid LIMIT {args.limit}" if args.limit else "ORDER BY l.guid")
    log(f"era {e['name']}; {len(rows)} characters without traits; lanes: "
        + ", ".join(f"{l.name}x{l.slots}" for l in lanes))
    pool = fleet.Pool(lanes, log=log)
    rng = random.Random()
    for c in rows:
        pool.submit("traits", 1, lambda lane, attempt, c=c: gen_traits(lane, attempt, e, judge, c, rng),
                    f"traits {c['guid']} {c['name']}")
    failures = pool.run()
    for j in failures:
        log(f"FAILED {j['label']}: {' | '.join(j['errors'])}")
    done = sql(f"SELECT COUNT(*) FROM lore_character WHERE era = {q(e['name'])} AND personality IS NOT NULL")[0][0]
    log(f"done: {done} characters have traits; {len(failures)} failed")
    return 1 if failures else 0


def sample_traits(args):
    e = eras.get(args.era)
    ensure_schema()
    lanes = [l for l in fleet.pick_lanes(args.lanes, args.slots) if "traits" in l.kinds]
    judge = fleet.Judge()
    rows = trait_rows(e, where="1 = 1", extra=f"ORDER BY RAND() LIMIT {len(lanes) * args.per_lane}")
    jobs = [(lanes[i % len(lanes)], c) for i, c in enumerate(rows)]
    rng = random.Random()

    def one(job):
        lane, c = job
        t0 = time.time()
        head = f"== {lane.name} ({{:.0f}}s) {c['name']}, {RACES[c['race']]} {CLASSES[c['cls']]}, level {c['level']}" \
               + (f", {c['guild']}" if c["guild"] else "")
        try:
            data, kinds, problems, flag = build_traits(lane, c, e, judge, rng)
        except Exception as ex:
            return head.format(time.time() - t0) + f"\n   FAILED {ex}"
        verdict = "; ".join(problems) or (f"judge: {flag}" if flag else "passes")
        t = data["motivation_target"]
        return (head.format(time.time() - t0) + f"\n   offered: {', '.join(kinds)} | check: {verdict}"
                f"\n   personality: {data['personality']}\n   gist: {data['gist']}"
                f"\n   motivation ({data['motivation_kind']} -> {t['type']}: {t['name']}): {data['motivation']}"
                f"\n   short: {data['motivation_short']}")

    with cf.ThreadPoolExecutor(len(jobs) or 1) as ex:
        for out in ex.map(one, jobs):
            print(out, flush=True)
    return 0


VARY_PROMPT = """You are revising one line about {name}, a {race} {cls} of the {faction} living in Azeroth.

Their personality line now: {personality}
Their story: {gist}

Too many people in this world are described with the same opening words. Rewrite the line so it keeps the same person (their traits, bearing, voice and telling habit) but begins differently and is worded freshly. Do not begin with any of these: {avoid}. No "like" comparison in the first clause. At most 40 words, speaking to them as "you" and "your", never "I", "me" or "my". Reply with only the new line."""

# Openings the models fall back on even when they aren't the most counted four words.
STOCK_OPENINGS = ["You move like", "You carry yourself like"]


# Plan 30 §2. Restoring the temperament line to every prompt exposed the characters whose personality was
# written against their own disposition: a SONGSMITH who "answers with silence, not words", a CAROUSER who
# speaks only when spoken to. The temperament is drawn first and the personality is meant to express it, so
# where they disagree it is the personality that came out wrong. Measured on the Classic corpus: 33 of the
# 363 characters with a talkative temperament, 9%.
TALKATIVE = {"CAROUSER", "CHARMER", "CHEERFUL", "GOSSIP", "JOKER", "SONGSMITH", "STORYTELLER",
             "YOUNG_RECRUIT", "HOTHEAD", "GLORY_SEEKER"}
TIGHT_LIPPED = re.compile(
    r"\b(speaks? little|says? little|rarely speaks?|seldom speaks?|answers? (questions )?with silence|"
    r"speaks? only when|words are few|few words|silence (is|as) (your|a) (wall|answer|shield)|"
    r"you do not speak|never speaks?|wordless|mute|taciturn|terse|curt|withdrawn|keeps? your own counsel|"
    r"guarded tongue|sparing with words|speaks? in whispers|whispers? (only|constantly))\b", re.I)

RECONCILE_PROMPT = """You are revising one line about {name}, a {race} {cls} of the {faction} living in Azeroth.

What this person is like, which is settled and not in question: {temperament}

How they are described now: {personality}

The description contradicts what they are like: it says they barely speak, while they are someone who does.
Rewrite it so it fits the disposition above. Keep every concrete thing in it - the habits, the objects they
carry, the scars, the places and the people named, the fears - and change only how they are said to speak
and carry themselves, so that a person reading it would recognise the disposition. Do not name the
disposition or use its word; show it. At most 45 words, speaking to them as "you" and "your", never "I",
"me" or "my". Reply with only the new description."""


def reconcile(args):
    """Rewrite personalities that fight their own temperament (plan 30 §2)."""
    e = eras.get(args.era)
    tempers = {t[0]: t[1] for t in temperaments()}
    rows = [dict(guid=int(r[0]), name=r[1], race=int(r[2]), cls=int(r[3]), temperament=r[4], personality=r[5])
            for r in sql("SELECT l.guid, c.name, c.race, c.class, l.temperament, "
                         f"{_flat('l.personality')} FROM lore_character l JOIN characters c ON c.guid = l.guid "
                         f"WHERE l.era = {q(e['name'])} AND l.personality IS NOT NULL ORDER BY l.guid")]
    todo = [c for c in rows if c["temperament"] in TALKATIVE and TIGHT_LIPPED.search(c["personality"])]
    log(f"{len(rows)} characters; {len(todo)} whose description fights their temperament")
    if args.dry_run:
        for c in todo:
            log(f"  [{c['temperament']}] {c['name']}: {c['personality'][:110]}")
        return 0
    lanes = [l for l in fleet.pick_lanes(args.lanes, args.slots) if "traits" in l.kinds]

    def job(c):
        def run(lane, attempt):
            prompt = RECONCILE_PROMPT.format(name=c["name"], race=RACES.get(c["race"], "person"),
                                             cls=CLASSES.get(c["cls"], "wanderer"),
                                             faction="Horde" if c["race"] in HORDE else "Alliance",
                                             temperament=tempers.get(c["temperament"], ""),
                                             personality=c["personality"])
            text = re.sub(r"\s+", " ", lane.chat([{"role": "user", "content": prompt}], 200, temperature=0.9)
                          .translate(ASCII_PUNCT)).strip().strip('"')
            words = _words(text)
            if not 8 <= words <= 55:
                raise ValueError(f"{words} words")
            if FIRST_PERSON.search(text) or not SECOND_PERSON.search(text):
                raise ValueError(f"voice: {text[:40]!r}")
            # "You speak little, but when song rises in your chest ..." resolves the contradiction rather
            # than repeating it, and a flat rule rejected three good lines in a row for one SONGSMITH.
            m = TIGHT_LIPPED.search(text)
            if m and not re.match(r"[^.]{0,40}\b(but|yet|until|unless|except|save when|when)\b", text[m.end():], re.I):
                raise ValueError(f"still tight-lipped: {text[:50]!r}")
            bad = e["meta"].search(text)
            if bad:
                raise ValueError(f"out-of-world term: {bad.group(0)!r}")
            sql(f"UPDATE lore_character SET personality = {q(text[:400])} WHERE guid = {c['guid']}", fetch=False)
            return text[:40]
        return run

    pool = fleet.Pool(lanes, log=lambda m: log(m) if "FAILED" in m or "pool finished" in m else None)
    for c in todo:
        pool.submit("traits", 1, job(c), f"reconcile {c['guid']} {c['name']}")
    failures = pool.run()
    log(f"done: {len(todo) - len(failures)} rewritten, {len(failures)} failed")
    return 1 if failures else 0


# Plan 37 §5 P5. The 33 temperament templates are innocent - they read well, and only VENGEFUL,
# WAR_WEARY and GRIEVING are grim by design. What is grim is the personality line written underneath
# them. Measured 2026-09-21 over 1,798 party lines, grim vocabulary ran 47-81% in *every* temperament:
# CHEERFUL's Jaynia says "The world is dark", JOKER's Jestereno says "Stonetalon is ash and bone". The
# disposition is audible and then overwhelmed. Across the cast, 760 of 1,283 (59.2%) personalities carry
# a grim word and 181 (14.1%) carry any word of warmth - plan 30 §2's oldest open finding.
WARM_TEMPERAMENTS = {"CAROUSER", "CHARMER", "CHEERFUL", "DEVOUT", "GENTLE", "GLORY_SEEKER",
                     "GOSSIP", "HOTHEAD", "JOKER", "SONGSMITH", "STORYTELLER", "YOUNG_RECRUIT"}
# Deliberately absent: VENGEFUL, WAR_WEARY and GRIEVING. Their grimness is what they are for, and
# softening it would be a bug rather than a fix.

# The word boundaries are load-bearing, and cost six characters when they were missing: MySQL's REGEXP
# matches substrings, so the first count of this set caught "brother", "Azeroth", "Orgrimmar", "brash"
# and "flashing", and read 249 where the honest number is 243.
_GRIM_WORDS = ("grim", "silen", "stoic", "terse", "dead", "death", "rot", "grave", "blood",
               "dark", "cold", "shadow", "ash", "ruin", "bitter", "doom", "sorrow", "grief")
GRIM_PERSONALITY = re.compile(r"\b(" + "|".join(w + r"\w*" for w in _GRIM_WORDS) + r")\b", re.I)

SOFTEN_PROMPT = """You are revising one line about {name}, a {race} {cls} of the {faction} living in Azeroth.

What this person is like, which is settled and not in question: {temperament}

How they are described now: {personality}

The description is written in the same bleak register as nearly everyone else's, and it buries the
disposition above. Rewrite it so the disposition comes through. Keep every concrete thing in it - the
habits, the objects they carry, the scars, the places and the people named, the fears - and change only
the register: what they notice first, how they speak of it, what they do with their hands. The world is
still at war and this is not a happy person: do not make them cheerful, do not hand them a comfort they
have not earned, and do not take away a loss. Show the disposition rather than naming it, and do not use
its word. At most 45 words, speaking to them as "you" and "your", never "I", "me" or "my". Reply with
only the new description."""


def soften(args):
    """Rewrite grim personalities written under a warm temperament (plan 37 §5 P5)."""
    e = eras.get(args.era)
    tempers = {t[0]: t[1] for t in temperaments()}
    rows = [dict(guid=int(r[0]), name=r[1], race=int(r[2]), cls=int(r[3]), temperament=r[4], personality=r[5])
            for r in sql("SELECT l.guid, c.name, c.race, c.class, l.temperament, "
                         f"{_flat('l.personality')} FROM lore_character l JOIN characters c ON c.guid = l.guid "
                         f"WHERE l.era = {q(e['name'])} AND l.personality IS NOT NULL ORDER BY l.guid")]
    todo = [c for c in rows
            if c["temperament"] in WARM_TEMPERAMENTS and GRIM_PERSONALITY.search(c["personality"])]
    log(f"{len(rows)} characters; {len(todo)} with a warm temperament and a grim description")
    if args.limit:
        todo = todo[:args.limit]
        log(f"limited to {len(todo)}")
    if args.dry_run:
        for c in todo[:args.show]:
            log(f"  [{c['temperament']}] {c['name']}: {c['personality'][:110]}")
        if len(todo) > args.show:
            log(f"  ... and {len(todo) - args.show} more")
        return 0
    lanes = [l for l in fleet.pick_lanes(args.lanes, args.slots) if "traits" in l.kinds]

    def job(c):
        def run(lane, attempt):
            prompt = SOFTEN_PROMPT.format(name=c["name"], race=RACES.get(c["race"], "person"),
                                          cls=CLASSES.get(c["cls"], "wanderer"),
                                          faction="Horde" if c["race"] in HORDE else "Alliance",
                                          temperament=tempers.get(c["temperament"], ""),
                                          personality=c["personality"])
            text = re.sub(r"\s+", " ", lane.chat([{"role": "user", "content": prompt}], 200, temperature=0.9)
                          .translate(ASCII_PUNCT)).strip().strip('"')
            words = _words(text)
            if not 8 <= words <= 55:
                raise ValueError(f"{words} words")
            if FIRST_PERSON.search(text) or not SECOND_PERSON.search(text):
                raise ValueError(f"voice: {text[:40]!r}")
            # The whole point of the pass: a rewrite still wall-to-wall grim has done nothing. One grim
            # word left in a line that now carries the disposition is fine - this is range, not cheer.
            if len(GRIM_PERSONALITY.findall(text)) >= len(GRIM_PERSONALITY.findall(c["personality"])):
                raise ValueError(f"no less grim: {text[:50]!r}")
            bad = e["meta"].search(text)
            if bad:
                raise ValueError(f"out-of-world term: {bad.group(0)!r}")
            sql(f"UPDATE lore_character SET personality = {q(text[:400])} WHERE guid = {c['guid']}", fetch=False)
            return text[:40]
        return run

    pool = fleet.Pool(lanes, log=lambda m: log(m) if "FAILED" in m or "pool finished" in m else None)
    for c in todo:
        pool.submit("traits", 1, job(c), f"soften {c['guid']} {c['name']}")
    failures = pool.run()
    log(f"done: {len(todo) - len(failures)} rewritten, {len(failures)} failed")
    log("run `project` next, then `.ollama reload`: personality edits do not reach a prompt on their own")
    return 1 if failures else 0


def vary_personality(args):
    """Rewrite personality lines that share an opening with many others (plan 15 diversity pass)."""
    e = eras.get(args.era)
    base = f"FROM lore_character l WHERE l.era = {q(e['name'])} AND l.personality IS NOT NULL"
    common = [r[0] for r in sql(f"SELECT SUBSTRING_INDEX(l.personality, ' ', 4) o, COUNT(*) {base} "
                                f"GROUP BY o HAVING COUNT(*) >= {args.min_repeats} ORDER BY 2 DESC")]
    avoid = common + STOCK_OPENINGS
    rows = [dict(guid=int(r[0]), name=r[1], race=int(r[2]), cls=int(r[3]), personality=r[4], gist=r[5])
            for r in sql("SELECT l.guid, c.name, c.race, c.class, "
                         f"{_flat('l.personality')}, {_flat('l.gist')} FROM lore_character l JOIN characters c ON c.guid = l.guid "
                         f"WHERE l.era = {q(e['name'])} AND l.personality IS NOT NULL ORDER BY l.guid")]
    kept, todo = {}, []
    for c in rows:
        opening = next((a for a in avoid if c["personality"].lower().startswith(a.lower())), None)
        if opening is None:
            continue
        kept[opening] = kept.get(opening, 0) + 1
        if kept[opening] > args.keep:
            todo.append(c)
    log(f"{len(common)} common openings (>= {args.min_repeats}); rewriting {len(todo)} lines, keeping {args.keep} of each")
    lanes = [l for l in fleet.pick_lanes(args.lanes, args.slots) if "traits" in l.kinds]
    avoid_text = "; ".join(f'"{a}"' for a in avoid)

    def job(c):
        def run(lane, attempt):
            prompt = VARY_PROMPT.format(name=c["name"], race=RACES.get(c["race"], "person"),
                                        cls=CLASSES.get(c["cls"], "wanderer"),
                                        faction="Horde" if c["race"] in HORDE else "Alliance",
                                        personality=c["personality"], gist=c["gist"], avoid=avoid_text)
            text = re.sub(r"\s+", " ", lane.chat([{"role": "user", "content": prompt}], 160, temperature=0.9)
                          .translate(ASCII_PUNCT)).strip().strip('"')
            n = _words(text)
            if not 8 <= n <= 50:
                raise ValueError(f"{n} words")
            if FIRST_PERSON.search(text) or not SECOND_PERSON.search(text):
                raise ValueError(f"voice: {text[:40]!r}")
            if any(text.lower().startswith(a.lower()) for a in avoid):
                raise ValueError(f"same opening: {text[:30]!r}")
            bad = e["meta"].search(text)
            if bad:
                raise ValueError(f"out-of-world term: {bad.group(0)!r}")
            sql(f"UPDATE lore_character SET personality = {q(text[:400])} WHERE guid = {c['guid']}", fetch=False)
            return text[:40]
        return run

    pool = fleet.Pool(lanes, log=lambda m: log(m) if "FAILED" in m or "pool finished" in m or "slots" in m else None)
    for c in todo:
        pool.submit("traits", 1, job(c), f"vary {c['guid']} {c['name']}")
    failures = pool.run()
    log(f"done: {len(todo) - len(failures)} lines rewritten, {len(failures)} failed")
    return 1 if failures else 0


VARY_GIST_PROMPT = """You are revising one line about {name}, a {race} {cls} of the {faction} living in Azeroth.

Their story:
{backstory}

Their story in brief, as it reads now: {gist}

Too many people in this world have their lives summed up in the same words. Rewrite the brief version so it keeps the same life - the same names, the same one event that marked them - but opens differently and is worded freshly. Do not begin with any of these: {avoid}. Do not open on a birth or a birthplace; begin from whatever weighs most on them now. At most 45 words, speaking to them as "you" and "your", never "I", "me" or "my". Reply with only the new line."""

VARY_MOTIVATION_PROMPT = """You are revising one line about {name}, a {race} {cls} of the {faction} living in Azeroth.

What they are living for, in full:
{motivation}

That same drive in one line, as it reads now: {motivation_short}

Too many people in this world want things in the same words. Rewrite the one-line version so it keeps exactly the same goal, the same reason and the same stakes, but opens differently and is worded freshly. Do not begin with any of these: {avoid}. Lead with the thing itself, the place, the debt or the person where that reads better. One sentence, at most 20 words, saying "you" or "your" somewhere in it, never "I", "me" or "my". Reply with only the new line."""

VARY_BACKSTORY_PROMPT = """You are revising the opening of one story about {name}, a {race} {cls} of the {faction} living in Azeroth.

Their story as it reads now:
{backstory}

Too many people in this world have their stories told in the same shape. Rewrite it so it keeps the same life - every name, place, loss and event exactly as they are, and the same length - but opens differently and reads freshly. Do not begin with any of these: {avoid}. Vary the shape of the first sentence: it must not be "You" followed by a verb, and must not open on a scar, a habit, a kept object or a remembered smell. Speak to them as "you" and "your" throughout, never "I", "me" or "my". Reply with only the rewritten story."""

# gist and motivation_short are the fields mod-ollama-chat puts in *every* prompt (project()'s
# BIO_ core tier, alongside personality); backstory is the BIOX_ direct-address tier and is opt-in
# here because sweeping it is the largest run. vary-personality covers the third core field.
VARY_CRAFT_PROMPT = """You are revising one line about {name}, a {race} {cls} of the {faction} living in Azeroth.

The work of their hands, in full:
{craft}

That same trade in one line, as it reads now: {craft_short}

Too many people in this world name their work in the same words. Rewrite the one-line version so it keeps exactly the same trade and the same detail, but opens differently and is worded freshly. Do not begin with any of these: {avoid}. At most 18 words, saying "you" or "your" somewhere in it, never "I", "me" or "my", and no figures. Reply with only the new line."""

VARY_KIN_PROMPT = """You are revising one line about {name}, a {race} {cls} of the {faction} living in Azeroth.

Their people, in full:
{kin}

That same household in one line, as it reads now: {kin_short}

Too many people in this world speak of their families in the same words. Rewrite the one-line version so it keeps exactly the same people, the same number of them and the same place, but opens differently and is worded freshly. Do not begin with any of these: {avoid}. Nobody the full version leaves dead may come back alive. At most 18 words, saying "you" or "your" somewhere in it, never "I", "me" or "my", and write any count as a word. Reply with only the new line."""

VARY_FIELDS = {
    "backstory": dict(prompt=VARY_BACKSTORY_PROMPT, low=70, high=220, width=4000,
                      stock=STOCK_BACKSTORY_RE, extra=(),
                      seeds=["You were born", "You still", "You return", "You keep", "You trace",
                             "Your left", "Your right", "Your fingers", "Your knuckles", "The scar",
                             "You kneel", "You walk", "You sleep", "You wake"]),
    "gist": dict(prompt=VARY_GIST_PROMPT, low=15, high=60, width=600, stock=STOCK_GIST_RE,
                 extra=("backstory",), seeds=["Born", "Raised", "You were born", "You grew up"]),
    "motivation_short": dict(prompt=VARY_MOTIVATION_PROMPT, low=5, high=26, width=255,
                             stock=STOCK_MOTIVATION_RE, extra=("motivation",),
                             seeds=["You seek", "You strive", "You mean", "You aim", "You hunt",
                                    "You want", "You wish", "You long", "You live for"]),
    "craft_short": dict(prompt=VARY_CRAFT_PROMPT, low=5, high=24, width=255, stock=None,
                        extra=("craft",), seeds=[]),
    "kin_short": dict(prompt=VARY_KIN_PROMPT, low=5, high=24, width=255, stock=None,
                      extra=("kin",), seeds=[]),
}


def vary_traits(args):
    """Rewrite gist / motivation_short lines that share an opening with many others.

    Same shape as vary_personality, for the other two fields of the every-prompt core tier.
    A rewrite must keep the names its source text turns on, so the story stays the same story.
    """
    e = eras.get(args.era)
    fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    unknown = [f for f in fields if f not in VARY_FIELDS]
    if unknown:
        sys.exit(f"unknown field {unknown[0]!r}; choose from {', '.join(VARY_FIELDS)}")
    lanes = [l for l in fleet.pick_lanes(args.lanes, args.slots) if "traits" in l.kinds]
    rc = 0
    for field in fields:
        spec = VARY_FIELDS[field]
        cols = (field,) + spec["extra"]
        base = f"FROM lore_character l WHERE l.era = {q(e['name'])} AND l.{field} IS NOT NULL"
        common = [r[0] for r in sql(f"SELECT SUBSTRING_INDEX(l.{field}, ' ', {args.words}) o, COUNT(*) {base} "
                                    f"GROUP BY o HAVING COUNT(*) >= {args.min_repeats} ORDER BY 2 DESC")]
        avoid = common + spec["seeds"]
        rows = [dict(guid=int(r[0]), name=r[1], race=int(r[2]), cls=int(r[3]), **dict(zip(cols, r[4:])))
                for r in sql(f"SELECT l.guid, c.name, c.race, c.class, "
                             + ", ".join(_flat(f"l.{c}") for c in cols) + " "
                             "FROM lore_character l JOIN characters c ON c.guid = l.guid "
                             f"WHERE l.era = {q(e['name'])} AND l.{field} IS NOT NULL ORDER BY l.guid")]
        kept, todo = {}, []
        for c in rows:
            opening = next((a for a in avoid if c[field].lower().startswith(a.lower())), None)
            if opening is None:
                continue
            kept[opening] = kept.get(opening, 0) + 1
            if kept[opening] > args.keep:
                todo.append(c)
        if args.limit:
            todo = todo[:args.limit]
        log(f"{field}: {len(common)} openings shared by >= {args.min_repeats}; rewriting "
            f"{len(todo)} of {len(rows)} lines, keeping {args.keep} of each")
        if args.dry_run:
            for c in todo[:args.show]:
                print(f"== {c['name']}\n   now: {c[field]}")
            continue
        avoid_text = "; ".join(f'"{a}"' for a in avoid)
        # avoid is computed once, so the pass cannot see clusters it creates itself: left alone it
        # simply trades "You seek" for "You walk". Openings are counted as they are chosen, seeded
        # with the lines we are keeping, and a new one already this common is rejected and retried.
        todo_guids = {c["guid"] for c in todo}
        seen_openers, seen_lock = {}, threading.Lock()
        for c in rows:
            if c["guid"] not in todo_guids:
                head = " ".join(c[field].lower().split()[:args.words])
                seen_openers[head] = seen_openers.get(head, 0) + 1

        def job(c, spec=spec, field=field, avoid=avoid, avoid_text=avoid_text):
            def run(lane, attempt):
                fmt = dict(name=c["name"], race=RACES.get(c["race"], "person"),
                           cls=CLASSES.get(c["cls"], "wanderer"),
                           faction="Horde" if c["race"] in HORDE else "Alliance",
                           avoid=avoid_text, **{k: c[k] for k in (field,) + spec["extra"]})
                text = re.sub(r"\s+", " ", lane.chat([{"role": "user", "content": spec["prompt"].format(**fmt)}],
                                                     240, temperature=0.9).translate(ASCII_PUNCT)).strip().strip('"')
                n = _words(text)
                if not spec["low"] <= n <= spec["high"]:
                    raise ValueError(f"{n} words")
                if FIRST_PERSON.search(text) or not SECOND_PERSON.search(text):
                    raise ValueError(f"voice: {text[:40]!r}")
                # `stock` is None for a field with no measured stock opening yet (plan 33); the
                # avoid list, seeded from the run itself, is the only guard those fields need.
                if (spec["stock"] and spec["stock"].match(text)) \
                        or any(text.lower().startswith(a.lower()) for a in avoid):
                    raise ValueError(f"same opening: {text[:30]!r}")
                bad = e["meta"].search(text)
                if bad:
                    raise ValueError(f"out-of-world term: {bad.group(0)!r}")
                # One passing name may go; the people and places the line turns on may not.
                lost = sorted(p for p in proper_names(c[field])
                              if not re.search(rf"\b{re.escape(p)}\b", text))
                if len(lost) > 1:
                    raise ValueError(f"drops names: {', '.join(lost[:3])}")
                head = " ".join(text.lower().split()[:args.words])
                with seen_lock:
                    if seen_openers.get(head, 0) >= args.keep:
                        raise ValueError(f"opening already common in this run: {head!r}")
                    seen_openers[head] = seen_openers.get(head, 0) + 1
                sql(f"UPDATE lore_character SET {field} = {q(text[:spec['width']])} "
                    f"WHERE guid = {c['guid']}", fetch=False)
                return text[:40]
            return run

        pool = fleet.Pool(lanes, log=lambda m: log(m) if "FAILED" in m or "pool finished" in m or "slots" in m else None)
        for c in todo:
            pool.submit("traits", 1, job(c), f"vary {field} {c['guid']} {c['name']}")
        failures = pool.run()
        log(f"{field}: {len(todo) - len(failures)} rewritten, {len(failures)} failed")
        if failures:
            rc = 1
    return rc


def traits_report(args):
    e = eras.get(args.era)
    base = f"FROM lore_character l JOIN characters c ON c.guid = l.guid WHERE l.era = {q(e['name'])} AND l.personality IS NOT NULL"
    total = int(sql(f"SELECT COUNT(*) {base}")[0][0])
    print(f"{total} characters with traits")
    print("kinds:", ", ".join(f"{r[0]} {r[1]}" for r in sql(f"SELECT l.motivation_kind, COUNT(*) {base} GROUP BY 1 ORDER BY 2 DESC")))
    for race, label in sorted(RACES.items()):
        rows = sql(f"SELECT l.motivation_kind, COUNT(*) {base} AND c.race = {race} GROUP BY 1 ORDER BY 2 DESC LIMIT 4")
        if rows:
            print(f"  {label:10}", ", ".join(f"{r[0]} {r[1]}" for r in rows))
    print("top targets:")
    for r in sql(f"SELECT JSON_UNQUOTE(JSON_EXTRACT(l.motivation_target, '$.name')), COUNT(*) {base} GROUP BY 1 ORDER BY 2 DESC LIMIT 15"):
        print(f"  {r[1]:4} {r[0]}")
    print("most repeated personality openings:")
    for r in sql(f"SELECT SUBSTRING_INDEX(l.personality, ' ', 4), COUNT(*) {base} GROUP BY 1 HAVING COUNT(*) > 3 ORDER BY 2 DESC LIMIT 10"):
        print(f"  {r[1]:4} {r[0]}")
    return 0


# ---------------------------------------------------------------------------
# the working life: a trade and a household (plan 33)

# The trade is not invented. Every bot already carries real profession skills -- 732 of the 1,283
# hold a full Classic gather+craft pairing, 221 hold one -- and nothing in mod-ollama-chat's source
# so much as names a skill, so not one of those trades has ever reached a prompt. This reads the
# trade out of character_skills and writes the life around it, which makes the claim true by
# construction: plan 25 §107's open question ("must a bot's claimed trade be real?") answered from
# the cheap side, with no mod-playerbots work at all.
PROFESSIONS = {
    164: ("smith", "the hammer, the anvil, quenching and tempering, and knowing a weld by its colour"),
    165: ("leatherworker", "hides, the tanning pit, the awl and waxed thread"),
    171: ("alchemist", "the alembic, distilling, measuring by the drop and by weight"),
    182: ("herb-gatherer", "which leaf is cut at dawn, which root rots if it travels wet"),
    186: ("miner", "the seam, the pick, bad air, and how long a prop will hold"),
    197: ("tailor", "the loom, the bolt, needle and shears"),
    202: ("tinker", "gears, springs, blasting powder, and things that fail loudly"),
    333: ("enchanter", "dust, and thread of magic worked into cloth and steel"),
    393: ("skinner", "the skinning knife, what a hide is worth, and what ruins one"),
    # Jewelcrafting and Inscription are trades of later ages. 117 bots hold the first and 104 the
    # second, in every case as the SECOND profession of a miner or a herb-gatherer -- which is why
    # exactly 221 characters come out of the count below with only one trade. Naming them in the
    # Classic era would plant precisely the anachronism the META sweep and the judge exist to catch,
    # so for that era they are never offered and those bots simply have one trade.
    755: ("jeweller", "the loupe, the wheel, stones cut and set"),
    773: ("scribe", "ink, vellum and a steady hand"),
}
PROF_ERA = {"classic": {164, 165, 171, 182, 186, 197, 202, 333, 393}, "wotlk": set(PROFESSIONS)}
# Every bot with a trade also has these three. They are the everyday half and are worth naming,
# because what a person does between fights is more of who they are than what they are titled.
SECONDARY = {185: "you cook for whoever you are travelling with",
             356: "you fish when there is water and time",
             129: "you know how to bind a wound"}

# Where a person's people are. Models misplace towns unless the prompt hands them the settlements
# (paid for twice in plan 14 B1), so each race is offered its own short list and nothing else.
HOMELANDS = {
    1: "Stormwind City, Goldshire and the farms of Elwynn, Sentinel Hill or Moonbrook in Westfall, "
       "Lakeshire in Redridge, Darkshire in Duskwood, Southshore in Hillsbrad, Menethil Harbour in the "
       "Wetlands, Stromgarde in Arathi, Theramore across the sea, or among the refugees out of fallen "
       "Lordaeron",
    2: "Orgrimmar, Razor Hill in Durotar, the Crossroads or Camp Taurajo in the Barrens, Hammerfall in "
       "Arathi, or still remembered in the internment camps and in Lordaeron's ruin",
    3: "Ironforge, Kharanos in Dun Morogh, Thelsamar in Loch Modan, Menethil Harbour in the Wetlands, "
       "the prospectors' camps in the Badlands, or Aerie Peak among the Wildhammer",
    4: "Darnassus, Dolanaar on Teldrassil, Auberdine in Darkshore, Astranaar in Ashenvale, Feathermoon "
       "Stronghold in Feralas, or the old groves of Ashenvale that burned",
    5: "the Undercity beneath Lordaeron, Brill or Deathknell in Tirisfal, the Sepulcher in Silverpine, "
       "Tarren Mill in Hillsbrad -- or, still living and knowing nothing of you, in Southshore, "
       "Stormwind or wherever the living fled",
    6: "Thunder Bluff, Bloodhoof Village or Camp Narache in Mulgore, Camp Taurajo in the Barrens, or "
       "Ghost Walker Post in Desolace",
    7: "Ironforge's Tinker Town, Kharanos in Dun Morogh, or scattered out of Gnomeregan and not all of "
       "them accounted for",
    8: "Sen'jin Village in Durotar, the Valley of Spirits in Orgrimmar, the Echo Isles that the Darkspear "
       "lost, or the camps along the Barrens road",
}
# A uniform "how many sisters, where does your father work" would hand a Forsaken rogue a father who
# still keeps bees. Kin means something different to each of these peoples and the prompt has to say
# so, or the field is lore-wrong for four races out of eight.
KIN_FRAMING = {
    1: "An ordinary human family: parents, brothers and sisters, an aunt or an uncle, some of them "
       "scattered by the wars and some of them still at work in the same town.",
    2: "Orc kin are clan as much as blood, and a generation of them grew up behind the fences of the "
       "internment camps; say which clan, and who came out of the camps with you.",
    3: "Dwarf families are large, loud and proud of a trade held for generations; clan and craft run "
       "together, and a cousin somewhere is always in the same line of work.",
    4: "Night elves measure kin in centuries. A parent may have stood at Hyjal, a sibling may be in the "
       "Sentinels or asleep in the Emerald Dream, and a child grown is still young to you.",
    5: "You are Forsaken, and your family is the family you had in life. Some are dead with you, some "
       "are dead and not with you, and some may still be living and would not have you at their door. "
       "Say plainly which -- do not give a dead man a shop to open in the morning.",
    6: "Tauren kin are the herd and the tribe: elders, a mother's line, children raised by many hands, "
       "and the dead honoured as still present.",
    7: "Gnomeregan scattered every gnome family at once. Some of your people got out, some are counted "
       "missing rather than dead, and the ones who got out are mostly in Tinker Town now.",
    8: "Darkspear kin are the village and the loa-honoured line: a mother's people, a grandmother who "
       "speaks for the dead, brothers and sisters raised together in the same hut.",
}

# Every land and settlement of the age, so a place used in the new text can be checked against the
# ones this person's people actually live in. Gustatinech, a tauren, was given a grandmother in the
# Blasted Lands on the first sample run -- the word appears nowhere in his story, and HOMELANDS only
# ever reached the household half of the prompt. Models misplace towns whenever the prompt does not
# fence them (plan 14 B1, twice); this is that fence, applied to the output as well as the prompt.
GAZETTEER = (
    "Stormwind", "Goldshire", "Elwynn", "Westfall", "Sentinel Hill", "Moonbrook", "Redridge", "Lakeshire",
    "Duskwood", "Darkshire", "Hillsbrad", "Southshore", "Tarren Mill", "Wetlands", "Menethil", "Arathi",
    "Stromgarde", "Hammerfall", "Theramore", "Kul Tiras", "Lordaeron", "Tirisfal", "Brill", "Deathknell",
    "Silverpine", "Sepulcher", "Undercity", "Ironforge", "Kharanos", "Dun Morogh", "Loch Modan",
    "Thelsamar", "Badlands", "Searing Gorge", "Burning Steppes", "Blackrock", "Aerie Peak", "Hinterlands",
    "Gnomeregan", "Tinker Town", "Darnassus", "Teldrassil", "Dolanaar", "Darkshore", "Auberdine",
    "Ashenvale", "Astranaar", "Felwood", "Winterspring", "Moonglade", "Feralas", "Feathermoon",
    "Desolace", "Ghost Walker", "Stonetalon", "Thousand Needles", "Dustwallow", "Tanaris", "Un'Goro",
    "Silithus", "Azshara", "Orgrimmar", "Durotar", "Razor Hill", "Sen'jin", "Echo Isles", "Barrens",
    "Crossroads", "Ratchet", "Camp Taurajo", "Mulgore", "Thunder Bluff", "Bloodhoof", "Camp Narache",
    "Stranglethorn", "Booty Bay", "Grom'gol", "Swamp of Sorrows", "Blasted Lands", "Plaguelands",
    "Andorhal", "Stratholme", "Scholomance", "Zul'Gurub", "Ahn'Qiraj", "Quel'Thalas", "Khaz Modan",
    "Kalimdor", "Hyjal", "Alterac", "Arathi Highlands", "Deadmines", "Dalaran",
)
GAZETTEER_RE = re.compile(r"\b(" + "|".join(re.escape(g) for g in GAZETTEER) + r")\b")


def misplaced(text, race, story):
    """Lands named in the new text that are neither this people's own nor already in their story."""
    home = HOMELANDS.get(race, "")
    return sorted({g for g in GAZETTEER_RE.findall(text) if g not in home and g not in story})


KIN_SINGULAR = {"mother": "mother", "mothers": "mother", "ma": "mother", "mum": "mother",
                "father": "father", "fathers": "father", "da": "father",
                "parent": "parents", "parents": "parents",
                "sister": "sister", "sisters": "sister", "brother": "brother", "brothers": "brother",
                "sibling": "sibling", "siblings": "sibling",
                "aunt": "aunt", "aunts": "aunt", "uncle": "uncle", "uncles": "uncle",
                "cousin": "cousin", "cousins": "cousin",
                "grandmother": "grandmother", "grandfather": "grandfather",
                "daughter": "daughter", "daughters": "daughter", "son": "son", "sons": "son",
                "wife": "wife", "husband": "husband", "family": "family", "kin": "kin"}
DEATH_WORDS = re.compile(
    r"\b(died|dies|dead|death|dying|killed|kills|slain|slew|murdered|perished|buried|burial|grave|graves|"
    r"corpse|corpses|bodies|pyre|ashes|widow\w*|orphan\w*|butchered|hanged|drowned|massacre\w*|"
    r"lost|gone|fell|fallen|taken|risen)\b", re.I)
# "raised" and "turned" were in that list and came out again, measured against all 1,283 stories.
# Both have a common meaning that is the OPPOSITE of the one wanted: "raised by your uncle and aunt"
# (65 characters) says the uncle was alive and rearing you, and "a stonemason turned archer" (67) is
# a change of trade. Everything else stays in deliberately, false positives included -- the two
# errors are not equal. Marking a living mother dead costs one household written around whoever
# else there is; missing a dead one puts a man the Scourge took behind a shop counter at dawn.


def kin_state(text, window=12):
    """Which kin the story has already spoken for, and which of them it buried.

    Handed "now write about their family", a model will cheerfully give someone a father who still
    keeps bees three lines after the story said the Scourge took him. The check is mechanical
    because it must run on all 1,283 and because the answer is not a matter of taste: a kin word
    with a death word within `window` words of it is a kin word the new text may not bring back.

    Same shape as proper_names(): a guard computed from the source, handed to the prompt AND
    checked against the output -- because a constraint stated in a prompt has never been enough on
    its own anywhere else in this file either.
    """
    words = text.split()
    low = [w.lower().strip(".,;:!?'\"()-") for w in words]
    state = {}
    for i, w in enumerate(low):
        role = KIN_SINGULAR.get(w)
        if not role:
            continue
        if DEATH_WORDS.search(" ".join(words[max(0, i - window):i + window + 1])):
            state[role] = "dead"
        else:
            state.setdefault(role, "living")
    return state


# Asked for 25-45, accepted at 20-70. Models overshoot a stated maximum, so one number cannot do
# both jobs (plan 21 §2). The gap is wide on purpose: the first samples came back at 68 and 79
# words carrying the best detail in them ("he said it kept the ghosts from tasting the fish"),
# and a cap below what the material needs makes a model throw facts away rather than write
# tighter. So the columns are 600 wide, not 400 -- at 400 a 70-word answer would have been
# truncated mid-sentence by the width cut below, silently. The ceiling then went 70 -> 80
# because the quality lane kept landing on 71 and 73: rejecting a good answer over one word
# buys nothing and costs a retry.
# Only the two long fields must say "you". The shorts are rendered behind a label that already
# supplies it ("Your people: Aunt Branna and two nephews in Kharanos"), and requiring it inside
# an eighteen-word fragment only makes the model pad a list that read better without it.
LIFE_ADDRESSED = {"craft", "kin"}
LIFE_WORDS = (("craft", 20, 80), ("craft_short", 5, 24), ("kin", 20, 80), ("kin_short", 5, 24))
LIFE_WIDTH = {"craft": 600, "craft_short": 255, "kin": 600, "kin_short": 255}
LIFE_COLUMNS = [
    ("craft", "VARCHAR(600) NULL COMMENT 'The work of your hands, ~45 words; read when addressed (plan 33)'"),
    ("craft_short", "VARCHAR(255) NULL COMMENT 'The trade in one line; every prompt (plan 33)'"),
    ("kin", "VARCHAR(600) NULL COMMENT 'Your people, ~45 words; read when addressed (plan 33)'"),
    ("kin_short", "VARCHAR(255) NULL COMMENT 'The household in one line; every prompt (plan 33)'"),
]

LIFE_PROMPT = """You are a loremaster for {setting} Everything you write is real life to the person in it. Never mention games, players, levels as numbers, specs, raids, loot, servers or anything outside the world.{limits}

This is {name}, a {gender} {race} {cls} of the {faction}, {standing}.{guild_line}
Temperament: {temperament}
Who they are in any room: {personality}
Their story, as told to them:
{backstory}
What they are living for: {motivation}

{trade_block}
Where their people would be: {homelands}.
{kin_framing}{dead_block}
Write four things about {name} that their story did not have room for. Every one speaks to {name} as "you" and "your": never "I", "me" or "my". Keep every name the story already uses and never introduce the names {overused}. Write any number as a word - "two sisters", "a brother and three cousins" - and give no ages, no prices and no counts of coin.
1. "craft": the work of your hands, 25 to 45 words. {trade_line} Say where you learned it and from whom - the place must be one of those named above, or somewhere your own story already names - one thing about the work that only someone who does it would know, and one thing you still carry or still do because of it. Ordinary and present tense: this is what you do in the weeks when nobody is fighting.
2. "craft_short": the same trade in at most 18 words, the way you would mention it in passing. Name the work itself, not only the habit that comes with it.
3. "kin": your people, 25 to 45 words. Say who they are and how many, where they are now, and one small true thing about them - what one of them does with their days, a thing one of them always says, something owed or sent between you. Small and specific beats sad and grand.
4. "kin_short": the same household in at most 18 words. Keep who they are, how many and where.

Nothing here may contradict the story above, and nothing here may repeat it in its own words: this is what was left out, not a summary of what was said.

Reply with ONLY a JSON object with the keys craft, craft_short, kin, kin_short. No markdown fences."""


def life_rows(e, where="l.craft IS NULL", extra=""):
    """Characters needing a working life, each with the professions they really hold."""
    rows = sql("SELECT l.guid, c.name, c.race, c.class, c.gender, c.level, "
               f"{_flat('t.prompt')}, {_flat('l.backstory')}, {_flat('l.personality')}, "
               f"{_flat('l.motivation')}, {_flat('g.name')}, {_flat('gr.rname')}, {_flat(ROLE_SQL)} "
               "FROM lore_character l JOIN characters c ON c.guid = l.guid "
               "LEFT JOIN mod_ollama_chat_personality_templates t ON t.`key` = l.temperament "
               "LEFT JOIN guild_member m ON m.guid = l.guid LEFT JOIN guild g ON g.guildid = m.guildid "
               "LEFT JOIN guild_rank gr ON gr.guildid = m.guildid AND gr.rid = m.rank "
               "LEFT JOIN lore_guild lg ON lg.guildid = m.guildid "
               f"WHERE l.era = {q(e['name'])} AND l.personality IS NOT NULL AND {where} "
               f"AND {era_filter(e)} {extra}")
    chars = [dict(guid=int(r[0]), name=r[1], race=int(r[2]), cls=int(r[3]), gender=int(r[4]),
                  level=int(r[5]), temperament=r[6], backstory=r[7], personality=r[8],
                  motivation=r[9], guild=r[10], rank=r[11], role=r[12], trades=[], secondary=[])
             for r in rows]
    if not chars:
        return chars
    # One query for every skill, joined in python: 1,283 round trips through the mysql CLI would
    # cost more than the generation does.
    by_guid = {c["guid"]: c for c in chars}
    legal = PROF_ERA.get(e["name"], set(PROFESSIONS))
    wanted = ",".join(str(s) for s in sorted(set(PROFESSIONS) | set(SECONDARY)))
    for r in sql(f"SELECT guid, skill FROM character_skills WHERE skill IN ({wanted}) ORDER BY guid, skill"):
        c = by_guid.get(int(r[0]))
        if not c:
            continue
        skill = int(r[1])
        if skill in SECONDARY:
            c["secondary"].append(SECONDARY[skill])
        elif skill in legal:
            c["trades"].append(PROFESSIONS[skill])
    return chars


def contradiction_prompt(story, text):
    """The judge lane, asked a different question.

    fleet.Judge parses one shape -- {"out_of_time": true, "evidence": ...} -- so a second kind of
    check reuses the key rather than the meaning. Ugly, and cheaper than a second judge class for
    a question asked once per character.
    """
    return ("Here are two pieces of writing about the same person. The first is their story and is "
            "the truth. The second was written afterwards and must not contradict it.\n\n"
            f"THE STORY:\n{story}\n\nWRITTEN AFTERWARDS:\n{text}\n\n"
            "Contradictions that matter: someone the story says is dead being alive, working or "
            "speaking; a different home, trade or family from the one the story gives; a name used "
            "for a different person than the story uses it for. Something the story simply does not "
            "mention is NOT a contradiction, and neither is a new detail that sits comfortably "
            "beside what the story says.\n"
            'Reply with only {"out_of_time": true, "evidence": "..."} if the second contradicts the '
            'first, quoting the two phrases that clash, or {"out_of_time": false} if it does not.')


def build_life(lane, c, e, judge):
    """One call for both fields. Returns (data, problems, flag); nothing is stored here.

    Trade and household go in one call because they are usually the same fact: the forge a smith
    learned at is his father's forge. Generated apart they agree only by luck, and it would cost
    twice the fleet time to get that worse result. They are stored in separate columns so either
    can be re-run or varied on its own.
    """
    trades = c["trades"]
    if trades:
        named = ", ".join(t[0] for t in trades)
        texture = "; ".join(f"{t[0]}: {t[1]}" for t in trades)
        trade_block = (f"The work {c['name']} is actually trained in: {named}. What that work involves - "
                       f"{texture}.")
        if c["secondary"]:
            trade_block += " Besides that, " + ", and ".join(c["secondary"]) + "."
        trade_line = f"You are a {named}."
    else:
        # 330 characters hold no profession rows at all. They still need a living, so the model is
        # asked for one -- but it must be work that leaves no skill to be caught missing: carrying,
        # digging, minding animals, standing a watch for pay.
        trade_block = (f"{c['name']} was never apprenticed to a trade.")
        if c["secondary"]:
            trade_block += " What they can do: " + ", and ".join(c["secondary"]) + "."
        trade_line = ("You were never apprenticed to anything; name the plain work you do for bread - "
                      "carrying, digging, minding beasts, standing a watch for pay - not a craft you "
                      "would have to have been taught.")
    buried = sorted(r for r, s in kin_state(c["backstory"]).items() if s == "dead")
    dead_block = ""
    if buried:
        dead_block = ("\nYour story has already buried these: " + ", ".join(buried) +
                      ". They stay dead. Speak of them only as remembered, and build the household "
                      "out of whoever else there is.")
    guild_line = ""
    if c["guild"]:
        guild_line = f" Sworn to the company \"{c['guild']}\" with the rank {c['rank']}. {c['role']}"
    prompt = LIFE_PROMPT.format(
        setting=e["setting"], limits=e["limits"], name=c["name"],
        gender="female" if c["gender"] else "male", race=RACES.get(c["race"], "unknown"),
        cls=CLASSES.get(c["cls"], "wanderer"), faction="Horde" if c["race"] in HORDE else "Alliance",
        standing=eras.standing(c["level"], e), guild_line=guild_line, temperament=c["temperament"],
        personality=c["personality"], backstory=c["backstory"], motivation=c["motivation"],
        trade_block=trade_block, trade_line=trade_line,
        homelands=HOMELANDS.get(c["race"], "wherever their people settled"),
        kin_framing=KIN_FRAMING.get(c["race"], ""), dead_block=dead_block,
        overused=", ".join(OVERUSED_NAMES))
    raw = extract_json(lane.chat([{"role": "user", "content": prompt}], 700, temperature=0.85))
    data = {k: re.sub(r"\s+", " ", str(raw.get(k) or "").translate(ASCII_PUNCT)).strip().strip('"')
            for k, _, _ in LIFE_WORDS}

    problems = []
    for key, low, high in LIFE_WORDS:
        n = _words(data[key])
        first = FIRST_PERSON.search(data[key])
        # The asked-for band is 25-55 and the accepted band is 20-70: a cap that fights the content
        # makes a model throw facts away rather than write tighter (plan 21 §2).
        if not low <= n <= high:
            problems.append(f"{key} has {n} words")
        elif first:
            problems.append(f"{key} slips into first person ({first.group(0)!r})")
        elif key in LIFE_ADDRESSED and not SECOND_PERSON.search(data[key]):
            problems.append(f"{key} never says you: {data[key][:40]!r}")
        elif re.search(r"\d", data[key]):
            # Invariant 1. A count of siblings is in-world and wanted; a figure is not.
            problems.append(f"{key} uses a figure, not a word: {data[key][:40]!r}")
    text = " ".join(data[k] for k, _, _ in LIFE_WORDS)
    revived = sorted(r for r, s in kin_state(text).items()
                     if s == "living" and kin_state(c["backstory"]).get(r) == "dead")
    if revived:
        problems.append(f"brings back the dead: {revived[0]}")
    introduced = {n for n in OVERUSED_RE.findall(text) if n not in c["backstory"]}
    if introduced:
        problems.append(f"introduces overused name {sorted(introduced)[0]!r}")
    bad = e["meta"].search(text)
    if bad:
        problems.append(f"out-of-world term: {bad.group(0)!r}")
    astray = misplaced(text, c["race"], c["backstory"])
    if astray:
        problems.append(f"{astray[0]} is not their people's land, nor in their story")

    flag = None
    if not problems and judge:
        flag = judge.check(eras.judge_prompt(e, text))
        if not flag:
            flag = judge.check(contradiction_prompt(c["backstory"], text))
    return data, problems, flag


def gen_life(lane, attempt, e, judge, c):
    data, problems, flag = build_life(lane, c, e, judge)
    if problems:
        raise ValueError(problems[0])
    if flag:
        if attempt < fleet.MAX_ATTEMPTS:
            raise ValueError(f"judge: {flag}")
        with _review_lock, open(os.path.join(LORE_DIR, f"review-{e['name']}.jsonl"), "a") as f:
            f.write(json.dumps({"label": f"life {c['guid']} {c['name']}", "evidence": flag, "text": data}) + "\n")
    sql("UPDATE lore_character SET "
        + ", ".join(f"{k} = {q(data[k][:LIFE_WIDTH[k]])}" for k, _, _ in LIFE_WORDS)
        + f" WHERE guid = {c['guid']}", fetch=False)
    return (c["trades"][0][0] if c["trades"] else "no trade")


def crafts(args):
    """A trade and a household for every character that lacks one (plan 33)."""
    e = eras.get(args.era)
    ensure_schema()
    lanes = [l for l in fleet.pick_lanes(args.lanes, args.slots) if "traits" in l.kinds]
    judge = None if args.no_judge else fleet.Judge()
    where = "1 = 1" if args.rewrite else "l.craft IS NULL"
    rows = life_rows(e, where=where,
                     extra=f"ORDER BY l.guid LIMIT {args.limit}" if args.limit else "ORDER BY l.guid")
    trained = sum(1 for c in rows if c["trades"])
    log(f"era {e['name']}; {len(rows)} characters without a working life ({trained} with a real trade, "
        f"{len(rows) - trained} without); lanes: " + ", ".join(f"{l.name}x{l.slots}" for l in lanes))
    pool = fleet.Pool(lanes, log=log)
    for c in rows:
        pool.submit("traits", 1, lambda lane, attempt, c=c: gen_life(lane, attempt, e, judge, c),
                    f"life {c['guid']} {c['name']}")
    failures = pool.run()
    for j in failures:
        log(f"FAILED {j['label']}: {' | '.join(j['errors'])}")
    done = sql(f"SELECT COUNT(*) FROM lore_character WHERE era = {q(e['name'])} AND craft IS NOT NULL")[0][0]
    log(f"done: {done} characters have a working life; {len(failures)} failed")
    return 1 if failures else 0


def sample_crafts(args):
    """Print a working life for random characters on each lane; stores nothing."""
    e = eras.get(args.era)
    ensure_schema()
    lanes = [l for l in fleet.pick_lanes(args.lanes, args.slots) if "traits" in l.kinds]
    judge = fleet.Judge()
    rows = life_rows(e, where="1 = 1", extra=f"ORDER BY RAND() LIMIT {len(lanes) * args.per_lane}")
    jobs = [(lanes[i % len(lanes)], c) for i, c in enumerate(rows)]

    def one(job):
        lane, c = job
        t0 = time.time()
        trades = ", ".join(t[0] for t in c["trades"]) or "no trade"
        head = (f"== {lane.name} ({{:.0f}}s) {c['name']}, {RACES[c['race']]} {CLASSES[c['cls']]}, "
                f"level {c['level']} [{trades}]")
        try:
            data, problems, flag = build_life(lane, c, e, judge)
        except Exception as ex:
            return head.format(time.time() - t0) + f"\n   FAILED {ex}"
        out = [head.format(time.time() - t0)]
        buried = sorted(r for r, s in kin_state(c["backstory"]).items() if s == "dead")
        if buried:
            out.append(f"   buried by the story: {', '.join(buried)}")
        for k, _, _ in LIFE_WORDS:
            out.append(f"   {k:12} {data[k]}")
        if problems:
            out.append(f"   PROBLEMS {'; '.join(problems)}")
        if flag:
            out.append(f"   JUDGE {flag}")
        return "\n".join(out)

    with cf.ThreadPoolExecutor(max_workers=len(jobs) or 1) as ex:
        for text in ex.map(one, jobs):
            print(text, flush=True)
    return 0


def unguilded_bots(e, extra=""):
    return [dict(guid=int(r[0]), name=r[1], race=int(r[2]), cls=int(r[3]), gender=int(r[4]),
                 level=int(r[5]), rank=None, rname=None)
            for r in sql("SELECT c.guid, c.name, c.race, c.class, c.gender, c.level FROM characters c "
                         "JOIN person_kind pk ON pk.guid = c.guid "
                         # Pool bots only, and deliberately not 'alt': an alt's story is built around a bond
                         # that `alts` writes first, so the bulk path must never claim one (plan 43 S1).
                         "WHERE pk.kind = 'bot' AND c.guid NOT IN (SELECT guid FROM guild_member) "
                         f"AND {era_filter(e)} {extra}")]


def generate(args):
    e = eras.get(args.era)
    ensure_schema()
    other = sql(f"SELECT (SELECT COUNT(*) FROM lore_guild WHERE era <> {q(e['name'])}) + "
                f"(SELECT COUNT(*) FROM lore_character WHERE era <> {q(e['name'])})")[0][0]
    if int(other):
        sys.exit(f"{other} lore rows belong to another era; run `reset` first")
    tempers = temperaments()
    lanes = fleet.pick_lanes(args.lanes, args.slots)
    judge = None if args.no_judge else fleet.Judge()
    pool = fleet.Pool(lanes, log=log)
    log(f"era {e['name']}; lanes: " + ", ".join(f"{l.name}x{l.slots}" for l in lanes)
        + ("; judge off" if not judge else "; judge evo-utility"))

    queued = {int(r[0]) for r in sql("SELECT guid FROM lore_character")}
    lock = threading.Lock()
    budget = [args.limit or 10 ** 9]
    over_cap = set()

    def queue_character(c, guild, prio):
        with lock:
            if c["guid"] in queued or budget[0] <= 0:
                return
            if c["level"] > e["max_level"]:
                # Waiting for a re-roll to the era's level range; a resumed run writes them.
                over_cap.add(c["guid"])
                return
            queued.add(c["guid"])
            budget[0] -= 1
        temper = random.choice(tempers)
        pool.submit("character", prio,
                    lambda lane, attempt: gen_character(lane, attempt, e, judge, c, temper, guild),
                    f"character {c['guid']} {c['name']}")

    def guild_job(gid, name):
        def run(lane, attempt):
            g = gen_guild(lane, attempt, e, judge, gid, name)
            if not g:
                return "(no members in this era, skipped)"
            for m in g["members"]:
                queue_character(m, g, 1)
            return f"-> {len(g['members'])} members queued"
        return run

    # Guilds already written for this era (a resumed run): queue their members now.
    for r in sql("SELECT l.guildid, g.name, l.history, l.member_roles FROM lore_guild l "
                 "JOIN guild g ON g.guildid = l.guildid"):
        g = dict(guildid=int(r[0]), name=r[1], history=r[2], roles=json.loads(r[3] or "{}"))
        g["members"] = roster(g["guildid"], e)
        for m in g["members"]:
            queue_character(m, g, 1)
    todo = sql("SELECT guildid, name FROM guild WHERE guildid NOT IN (SELECT guildid FROM lore_guild) "
               "ORDER BY guildid")
    for gid, name in todo:
        pool.submit("guild", 0, guild_job(int(gid), name), f"guild {gid} {name}")
    if args.unguilded:
        for c in unguilded_bots(e, "ORDER BY c.guid"):
            queue_character(c, None, 2)
    log(f"{len(todo)} guild histories queued (members follow each); {len(queued)} characters queued or done")

    failures = pool.run()
    for j in failures:
        log(f"FAILED {j['label']}: {' | '.join(j['errors'])}")
    n = sql(f"SELECT COUNT(*) FROM lore_character WHERE era = {q(e['name'])}")[0][0]
    g = sql(f"SELECT COUNT(*) FROM lore_guild WHERE era = {q(e['name'])}")[0][0]
    log(f"done: {g} guild histories, {n} backstories for era {e['name']}; {len(failures)} failed")
    if over_cap:
        log(f"{len(over_cap)} characters above level {e['max_level']} skipped until re-rolled; rerun to write them")
    return 1 if failures else 0


def sample(args):
    e = eras.get(args.era)
    tempers = temperaments()
    lanes = fleet.pick_lanes(args.lanes, args.slots)
    judge = fleet.Judge()
    bots = unguilded_bots(e, f"ORDER BY RAND() LIMIT {len(lanes)}")

    def one(lane, c):
        t0 = time.time()
        try:
            text = check_backstory(lane.chat([{"role": "user",
                                               "content": char_prompt(c, random.choice(tempers), None, e)}], 500))
        except Exception as ex:
            return f"== {lane.name}: {c['name']} FAILED {ex}"
        dt = time.time() - t0
        bad = e["meta"].search(text)
        verdict = judge.check(eras.judge_prompt(e, text))
        return (f"== {lane.name} ({dt:.0f}s) {c['name']}, {RACES[c['race']]} {CLASSES[c['cls']]} level {c['level']}\n"
                f"   regex: {bad.group(0) if bad else 'clean'}; judge: {verdict or 'clean'}\n   {text}")

    with cf.ThreadPoolExecutor(len(lanes)) as ex:
        for out in ex.map(one, lanes, bots):
            print(out, flush=True)
    return 0


def reset(args):
    host, port, user, pw, db = _DB
    os.makedirs(args.backup_dir, exist_ok=True)
    path = os.path.join(args.backup_dir, f"lore-before-reset-{time.strftime('%Y%m%d-%H%M%S')}.sql")
    with open(path, "w") as f:
        out = subprocess.run(["mysqldump", "--default-character-set=utf8mb4", "--single-transaction",
                              "--no-tablespaces", "-h", host, "-P", port, "-u", user, db,
                              "lore_guild", "lore_character", "mod_ollama_chat_personality_templates",
                              "mod_ollama_chat_personality"],
                             env={"MYSQL_PWD": pw, "PATH": "/usr/bin:/bin"}, stdout=f, stderr=subprocess.PIPE,
                             text=True)
    if out.returncode != 0:
        sys.exit(f"backup failed, nothing cleared: {out.stderr.strip()}")
    g = sql("SELECT COUNT(*) FROM lore_guild")[0][0]
    c = sql("SELECT COUNT(*) FROM lore_character")[0][0]
    sql("DELETE FROM lore_character; DELETE FROM lore_guild;", fetch=False)
    log(f"backed up to {path}; cleared {g} guild histories and {c} backstories "
        "(projections untouched until `project`)")
    return 0


# ---------------------------------------------------------------------------
# restand: stories fitted to where re-rolled characters stand now

RESTAND_PROMPT = """You are a loremaster for {setting} Everything you write is real life to the person in it. Never mention games, players, levels as numbers, specs, raids, loot, servers or anything outside the world.{limits}

These texts describe {name}, a {gender} {race} {cls} of the {faction}, who is now {standing}.{evidence}

Revise them so each fits someone who is {standing}. Change only what must change: how tested, capable and travelled {name} is now, and deeds they could not have done yet (turn those into what they mean or hope to do). Keep every name, place, person, loss and event that still fits, speak to {name} as "you" throughout (never "I", "me" or "my"), and keep each text about as long as it is.

{texts}

Reply with ONLY a JSON object with the keys backstory, gist, personality, motivation, motivation_short. No markdown fences."""

RESTAND_FIELDS = ("backstory", "gist", "personality", "motivation", "motivation_short")
STAND_LEVELS = (1, 10, 30, 60, 70)   # one level inside each of era.standing's bands
# A character's first level change after its story was written starts from the level the story was written for
# (the ledger has level_up rows for guild members and real players).
WRITTEN_LEVEL_SQL = ("SELECT e.actor_guid, JSON_EXTRACT(e.detail, '$.old') FROM ledger_event e JOIN ("
                     "SELECT x.actor_guid, MIN(x.id) AS first_id FROM ledger_event x JOIN lore_character l "
                     "ON l.guid = x.actor_guid WHERE x.event_type = 'level_up' AND x.ts > l.generated_at "
                     "GROUP BY x.actor_guid) f ON f.first_id = e.id")


def stand_ladder(e):
    """era.standing's bands for the era, greenest first."""
    ladder = []
    for level in STAND_LEVELS:
        if level <= e["max_level"] and eras.standing(level, e) not in ladder:
            ladder.append(eras.standing(level, e))
    return ladder


def stand_step(level, e):
    return stand_ladder(e).index(eras.standing(level, e))


def stand_phrases(e):
    """The standing words the generation prompts gave (each band's first four words), as texts may still carry them."""
    return [re.compile(re.escape(" ".join(s.split()[:4])), re.I) for s in stand_ladder(e)]


def proper_names(text):
    """Capitalised words that don't open a sentence: the names a revision must keep."""
    names = set()
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        for word in re.findall(r"[A-Za-z][A-Za-z']*", sentence)[1:]:
            word = re.sub(r"'s?$", "", word)
            if word and word[0].isupper() and word not in ("I", "You", "Your"):
                names.add(word)
    return names


def restand_rows(e):
    rows = sql("SELECT l.guid, c.name, c.race, c.class, c.gender, c.level, COALESCE(l.standing_level, 0), "
               + ", ".join(_flat(f"l.{f}") for f in RESTAND_FIELDS) + " "
               "FROM lore_character l JOIN characters c ON c.guid = l.guid "
               f"WHERE l.era = {q(e['name'])} AND l.personality IS NOT NULL AND {era_filter(e)} ORDER BY l.guid")
    return [dict(guid=int(r[0]), name=r[1], race=int(r[2]), cls=int(r[3]), gender=int(r[4]), level=int(r[5]),
                 standing_level=int(r[6]), **dict(zip(RESTAND_FIELDS, r[7:12]))) for r in rows]


def build_restand(lane, c, e, keep):
    """One revision call, checked. Returns (data, problems); nothing is stored here."""
    prompt = RESTAND_PROMPT.format(
        setting=e["setting"], limits=e["limits"], name=c["name"], gender="female" if c["gender"] else "male",
        race=RACES.get(c["race"], "unknown"), cls=CLASSES.get(c["cls"], "wanderer"),
        faction="Horde" if c["race"] in HORDE else "Alliance", standing=eras.standing(c["level"], e),
        evidence=c["evidence"], texts=json.dumps({f: c[f] for f in RESTAND_FIELDS}, indent=1, ensure_ascii=False))
    raw = extract_json(lane.chat([{"role": "user", "content": prompt}], 1200, temperature=0.6))
    data = {f: re.sub(r"\s+", " ", str(raw.get(f) or "").translate(ASCII_PUNCT)).strip().strip('"') for f in RESTAND_FIELDS}
    problems = []
    try:
        data["backstory"] = check_backstory(data["backstory"])
    except ValueError as ex:
        problems.append(f"backstory: {ex}")
    for key, low, high in TRAIT_WORDS:
        high = max(high, _words(c[key]) + 5)   # some texts were written past today's limits
        n, first = _words(data[key]), FIRST_PERSON.search(data[key])
        if not low <= n <= high:
            problems.append(f"{key} has {n} words")
        elif first:
            problems.append(f"{key} slips into first person ({first.group(0)!r})")
    # Second person is still required, but not as a mandated first word: restand must not
    # quietly re-impose "You seek ..." on a line vary-traits has just diversified.
    if data["motivation_short"] and not SECOND_PERSON.search(data["motivation_short"]):
        problems.append("motivation_short never says you")
    if data["motivation_short"] and STOCK_MOTIVATION_RE.match(data["motivation_short"]):
        problems.append("motivation_short uses a stock opening")
    if data["gist"] and STOCK_GIST_RE.match(data["gist"]):
        problems.append("gist uses a stock opening")
    old_all, new_all = " ".join(c[f] for f in RESTAND_FIELDS), " ".join(data.values())
    # One passing name may go (a recruit who has not met Thrall); a character of this world never.
    lost = sorted(n for n in proper_names(old_all) if not re.search(rf"\b{re.escape(n)}\b", new_all))
    if len(lost) > 1 or any(n in keep for n in lost):
        problems.append(f"drops names: {', '.join(lost[:4])}")
    if c.get("stale") and re.search(re.escape(c["stale"]), new_all, re.I):
        problems.append(f"still says {c['stale']!r}")
    if re.search(r"\d", new_all):
        problems.append("has a figure")
    introduced = {n for n in OVERUSED_RE.findall(new_all) if n not in old_all}
    if introduced:
        problems.append(f"introduces overused name {sorted(introduced)[0]!r}")
    bad = e["meta"].search(new_all)
    if bad:
        problems.append(f"out-of-world term: {bad.group(0)!r}")
    if not problems and all(data[f] == c[f] for f in RESTAND_FIELDS):
        problems.append("nothing changed")
    return data, problems


def gen_restand(lane, attempt, e, judge, c, dry_run, keep):
    data, problems = build_restand(lane, c, e, keep)
    if problems:
        raise ValueError(problems[0])
    vet(" ".join(data.values()), e, judge, attempt, f"restand {c['guid']} {c['name']}")
    if dry_run:
        log(f"== {c['name']}, now {eras.standing(c['level'], e)}.{c['evidence']}\n   gist was: {c['gist']}\n"
            f"   gist now: {data['gist']}\n   personality was: {c['personality']}\n   personality now: {data['personality']}")
        return "dry run"
    sql("UPDATE lore_character SET "
        + ", ".join(f"{f} = {q(data[f][:TRAIT_WIDTH.get(f, 60000)])}" for f in RESTAND_FIELDS)
        + f", standing_level = {c['level']} WHERE guid = {c['guid']}", fetch=False)
    return eras.standing(c["level"], e)


def restand(args):
    e = eras.get(args.era)
    ensure_schema()
    chars = restand_rows(e)
    if args.limit:
        chars = random.Random(args.seed).sample(chars, min(args.limit, len(chars)))
    keep = {r[0] for r in sql("SELECT name FROM characters")}
    judge = None if args.no_judge else fleet.Judge()
    now = lambda c: stand_step(c["level"], e)
    todo, fits, counts = [], [], dict(moved=0, ledger=0, words=0)
    for c in chars:
        if c["standing_level"] and abs(stand_step(c["standing_level"], e) - now(c)) >= args.gap:
            c["evidence"] = f" They were written for someone {eras.standing(c['standing_level'], e)}."
            todo.append(c)
            counts["moved"] += 1
    # Never fitted (written before standing_level existed). Where each stood when written: the ledger's first level
    # change after writing, or the standing words the prompt gave that the texts still carry. With neither, the texts
    # are taken as fitting and stamped with today's level, so later runs only look at characters moved since.
    written = {int(r[0]): int(float(r[1])) for r in sql(WRITTEN_LEVEL_SQL) if r[1] not in ("NULL", "")}
    phrases = stand_phrases(e)
    for c in chars:
        if c["standing_level"]:
            continue
        if c["guid"] in written:
            if abs(stand_step(written[c["guid"]], e) - now(c)) >= args.gap:
                c["evidence"] = f" They were written for someone {eras.standing(written[c['guid']], e)}."
                todo.append(c)
                counts["ledger"] += 1
            else:
                fits.append(c)
            continue
        text = " ".join(c[f] for f in RESTAND_FIELDS)
        said = [(step, m.group(0)) for step, rx in enumerate(phrases) for m in [rx.search(text)] if m]
        if said and said[0][0] != now(c):
            c["evidence"] = f" They still call {c['name']} \"{said[0][1]}\"."
            c["stale"] = said[0][1]
            todo.append(c)
            counts["words"] += 1
        else:
            fits.append(c)
    log(f"{len(chars)} characters: {counts['moved']} moved {args.gap}+ bands since fitted, {counts['ledger']} written "
        f"{args.gap}+ bands from where they stand (ledger), {counts['words']} still carrying another band's words; "
        f"{len(fits)} taken as fitting")
    if args.dry_run:
        for c in todo[:args.show]:
            log(f"   to revise: {c['name']} (level {c['level']}, now {eras.standing(c['level'], e)}).{c['evidence']}")
    else:
        for i in range(0, len(fits), 200):
            part = fits[i:i + 200]
            sql("UPDATE lore_character SET standing_level = CASE guid "
                + " ".join(f"WHEN {c['guid']} THEN {c['level']}" for c in part)
                + f" END WHERE guid IN ({','.join(str(c['guid']) for c in part)})", fetch=False)
    if args.check_only or not todo:
        return 0
    if args.dry_run:
        todo = todo[:args.show]
    lanes = [l for l in fleet.pick_lanes(args.lanes, args.slots) if "traits" in l.kinds]
    pool = fleet.Pool(lanes, log=lambda m: log(m) if "FAILED" in m or "pool finished" in m else None)
    for c in todo:
        pool.submit("traits", 1, lambda lane, attempt, c=c: gen_restand(lane, attempt, e, judge, c, args.dry_run, keep),
                    f"restand {c['guid']} {c['name']}")
    failures = pool.run()
    for j in failures:
        log(f"FAILED {j['label']}: {' | '.join(j['errors'])}")
    log(f"{'tried' if args.dry_run else 'revised'} {len(todo) - len(failures)} of {len(todo)}; {len(failures)} failed"
        + ("" if args.dry_run else "; run `project` to put them in prompts"))
    return 1 if failures else 0

# ---------------------------------------------------------------------------
# the player's alts (plan 21 §3-§4)

ALTS_DIR = os.path.join(LORE_DIR, "alts")

# What an alt may be to the main, and when it is allowed: an alt of another race cannot be its blood kin.
# `acquaintance` is ours (plan 21 §3, 2026-09-16): the one kind that carries no obligation, for a tie that is
# interest rather than loyalty.
BOND_KINDS = {
    "kin":          dict(need="race", weight=3, gloss="blood kin: a sibling, a cousin, or the child of a sibling"),
    "sworn":        dict(need="faction", weight=2, gloss="a shield-oath sworn between you"),
    "debt":         dict(need="faction", weight=2, gloss="a life saved and a debt not yet paid"),
    "mentor":       dict(need="faction", weight=1, gloss="you taught them something of your own craft"),
    "pupil":        dict(need="faction", weight=1, gloss="they taught you something of theirs"),
    "comrade":      dict(need="faction", weight=2, gloss="you served together in the same campaign"),
    "retainer":     dict(need="faction", weight=1, gloss="you serve their house or their cause"),
    "acquaintance": dict(need="faction", weight=2,
                         gloss="you met by chance, at work or on the road, and swore nothing to each other"),
    "distant":      dict(need="cross", weight=1,
                         gloss="you knew each other before the war, and it is not spoken of now"),
}

# Plan 21 §5. The bond is a story; this is the feeling it implies, so an alt walks up to its main already
# knowing them instead of as a stranger. `acquaintance` is 25 (2026-09-16): above `distant`, because they did
# choose to stay near, and well under the kinds that carry an obligation, because it swore nothing.
BOND_REGARD = {"kin": 60, "sworn": 55, "debt": 50, "mentor": 45, "pupil": 45,
               "comrade": 40, "retainer": 40, "acquaintance": 25, "distant": 10}
BOND_FAMILIARITY = 20   # moments a bond is worth, so the tie reads as old rather than met-today

BOND_PROMPT = """You are a loremaster for {setting} Everything you write is real life to the people in it. Never mention games, players, levels as numbers, specs, raids, loot, servers or anything outside the world.{limits}

{main_name} is a {main_gender} {main_race} {main_cls} of the {faction}. What is known of them:
{main_sheet}

Write for {name}, a {gender} {race} {cls} of the {faction}, {standing}.

What is between them: {gloss}.
{concept_block}{others_block}
Where the player's own words say what draws {name} to {main_name}, keep that reason exactly as they put it. Do not
soften it into something warmer or sadder, and do not replace it with grief, pity or admiration of character.
{keep_block}Write {lo} to {hi} words in the second person, addressed to {name}, saying what is between you and {main_name}.
Name {main_name}. Keep strictly to what is given above: invent no deeds for {main_name}, give them no words or
feelings that are not written, and settle nothing that is left open. Plain prose only: no title, no lists, no
quotes around it."""

BOND_WORDS = (30, 80)
BOND_TARGET = (40, 65)

# An alt's story carries more than a bot's: the player's own concept, and the bond to the main. Five straight
# attempts came in at 223-247 words against the 220 the plain bots are held to, so it gets its own band.
ALT_STORY_WORDS = (70, 265)


def check_bond(text, main_name, name):
    text = re.sub(r"\s+", " ", text).strip().strip('"')
    words = len(text.split())
    lo, hi = BOND_WORDS
    if not lo <= words <= hi:
        raise ValueError(f"{words} words")
    if main_name.lower() not in text.lower():
        raise ValueError(f"does not name {main_name}")
    if not SECOND_PERSON.search(text):
        raise ValueError("not second person")
    if re.search(r"\d", text):
        raise ValueError("carries a figure")
    if FIRST_PERSON.search(text):
        raise ValueError("slips into first person")
    return text


def allowed_kinds(alt, main):
    same_race = alt["race"] == main["race"]
    same_faction = (alt["race"] in HORDE) == (main["race"] in HORDE)
    out = []
    for kind, spec in BOND_KINDS.items():
        if spec["need"] == "race" and not same_race:
            continue
        if spec["need"] == "faction" and not same_faction:
            continue
        if spec["need"] == "cross" and same_faction:
            continue
        out.append(kind)
    return out


def main_of(alt_guid):
    rows = sql("SELECT pk.main_guid, c.name, c.race, c.class, c.gender, "
               f"{_flat('lm.sheet')}, COALESCE(lm.sheet_hash, '') "
               "FROM person_kind pk JOIN characters c ON c.guid = pk.main_guid "
               "LEFT JOIN lore_main lm ON lm.guid = pk.main_guid "
               f"WHERE pk.guid = {alt_guid}")
    if not rows or not rows[0][0]:
        return None
    r = rows[0]
    return dict(guid=int(r[0]), name=r[1], race=int(r[2]), cls=int(r[3]), gender=int(r[4]),
                sheet=r[5] or "", sheet_hash=r[6] or "")


def alt_rows(e, names=None, account=None):
    where = "pk.kind = 'alt'"
    if names:
        where += " AND c.name IN (" + ", ".join(q(n) for n in names) + ")"
    if account:
        # One household at a time. Without this a realm with two people writes both in a single run
        # (correct per alt, since main_of resolves each one, but impossible to do for just one) -- 43 H6.
        where += f" AND pk.account = {int(account)}"
    return [dict(guid=int(r[0]), name=r[1], race=int(r[2]), cls=int(r[3]), gender=int(r[4]), level=int(r[5]),
                 rank=None, rname=None)
            for r in sql("SELECT c.guid, c.name, c.race, c.class, c.gender, c.level "
                         "FROM person_kind pk JOIN characters c ON c.guid = pk.guid "
                         f"WHERE {where} AND {era_filter(e)} ORDER BY c.guid")]


# Words that must appear for a kind to have actually been written. A pinned `kin` bond that never says cousin,
# kin or blood is not a kin bond, whatever the column says.
# The match is a plain substring, not a whole word, so these are a loose guard rather than a proof: `kin` is
# inside "asking". They catch a bond that talks about something else entirely, which is what they are for.
# mentor/pupil share their words deliberately -- no list can tell which way the teaching ran, so the DIRECTION
# of those two is still unverified and wants a --keep anchor (plan 43 R2).
KIND_WORDS = {
    "kin": ("cousin", "kin", "blood", "brother", "sister", "family", "father", "mother", "uncle", "aunt"),
    "sworn": ("oath", "swore", "sworn", "vow"),
    "debt": ("debt", "owe", "owed", "saved", "repay"),
    "comrade": ("fought", "served", "campaign", "war", "battle"),
    "acquaintance": ("met", "knew", "chance", "work", "dock"),
    "mentor": ("taught", "teach", "learn", "apprentice", "master", "trained", "craft", "showed"),
    "pupil": ("taught", "teach", "learn", "apprentice", "master", "trained", "craft", "showed"),
    "retainer": ("serve", "service", "house", "cause", "loyal"),
    "distant": ("knew", "before", "unspoken", "not spoken", "silence", "once"),
}


def _shingles(text, n=6):
    words = re.findall(r"[a-z']+", text.lower())
    return {" ".join(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def check_bond_extra(text, kind, keep, other_bonds):
    """What check_bond cannot see: the kind actually written, the player's words, and plain copying."""
    need = KIND_WORDS.get(kind)
    if need and not any(w in text.lower() for w in need):
        raise ValueError(f"never says it is {kind} (looked for {', '.join(need[:4])}...)")
    missing = [k for k in keep if k.lower() not in text.lower()]
    if missing:
        raise ValueError("dropped: " + ", ".join(missing))
    # The earlier bonds are given as context so alts do not contradict each other. The models take them as a
    # template instead: one run produced two bonds that differed by a single verb, and the cousin's kinship
    # disappeared into the other alt's words.
    mine = _shingles(text)
    for other in other_bonds:
        shared = mine & _shingles(other)
        if shared:
            raise ValueError("reuses another bond's wording: " + repr(sorted(shared)[0]))
    return text


def build_bond(lane, c, m, kind, concept, others, e, keep=()):
    facts = m["sheet"] or (f"{m['name']} is spoken of little. Say nothing of their deeds, for none are known "
                           f"to you beyond the name and the face.")
    concept_block = f"\nWhat the player says is true of {c['name']}:\n{concept.strip()}\n" if concept else ""
    others_block = ""
    if others:
        # Only who they are and what they are to the main -- never their bond text. Handed the words, the models
        # copy them (see check_bond_extra).
        others_block = ("\nOthers are already bound to " + m["name"] + ". Do not contradict them, and do not reuse "
                        "their words, their images or their shape. What you write must sound like nobody else:\n"
                        + "\n".join(f"- {n}, who is their {k}" for n, k in others) + "\n")
    # Ask for the words the check will demand. Checking for words the prompt never requested is just guessing:
    # six straight attempts failed on "cousin" and "wealth" because nothing ever told the model to use them.
    keep_block = ("These exact words must appear in what you write: " + ", ".join(keep) + ".\n\n") if keep else ""
    prompt = BOND_PROMPT.format(
        keep_block=keep_block,
        setting=e["setting"], limits=e["limits"], main_name=m["name"],
        main_gender="female" if m["gender"] else "male", main_race=RACES.get(m["race"], "unknown"),
        main_cls=CLASSES.get(m["cls"], "adventurer"), main_sheet=facts,
        faction="Horde" if c["race"] in HORDE else "Alliance", name=c["name"],
        gender="female" if c["gender"] else "male", race=RACES.get(c["race"], "unknown"),
        cls=CLASSES.get(c["cls"], "adventurer"), standing=eras.standing(c["level"], e),
        gloss=BOND_KINDS[kind]["gloss"], concept_block=concept_block, others_block=others_block,
        lo=BOND_TARGET[0], hi=BOND_TARGET[1])
    raw = lane.chat([{"role": "user", "content": prompt}], 400, temperature=0.7)
    return check_bond(str(raw or "").translate(ASCII_PUNCT), m["name"], c["name"])


def gen_alt_backstory(lane, attempt, e, judge, c, temper, bond, m, concept="", keep=()):
    text = check_backstory(lane.chat(
        [{"role": "user", "content": char_prompt(c, temper, None, e, bond=bond, main_name=m["name"],
                                                 concept=concept, main_sheet=m["sheet"], keep=keep)}], 600),
        ALT_STORY_WORDS)
    if m["name"].lower() not in text.lower():
        raise ValueError(f"does not name {m['name']}")
    # Traits the player named belong here rather than in the bond: a bond says what is between two people, so
    # asking 40 words of it to also carry "wealth" is asking the wrong text.
    missing = [k for k in keep if k.lower() not in text.lower()]
    if missing:
        raise ValueError("dropped: " + ", ".join(missing))
    vet(text, e, judge, attempt, f"alt {c['guid']} {c['name']}")
    sql("INSERT INTO lore_character (guid, guildid, temperament, backstory, model, era) VALUES "
        f"({c['guid']}, NULL, {q(temper[0])}, {q(text)}, {q((lane.name + ':' + lane.model)[:64])}, "
        f"{q(e['name'])}) ON DUPLICATE KEY UPDATE backstory = VALUES(backstory), "
        f"temperament = VALUES(temperament), model = VALUES(model), era = VALUES(era)", fetch=False)
    return text


def first_sentence(text, width=255):
    """The bond's opening sentence: the alt's own account of what is between them, in its own words."""
    text = re.sub(r"\s+", " ", text or "").strip()
    m = re.match(r"(.+?[.!?])(?:\s|$)", text)
    return (m.group(1) if m else text).strip()[:width]


def seed_bond_regard(e, guids=None, dry_run=False):
    """Plan 21 P5: turn each written bond into regard(alt -> main), and half of it between the alts themselves.

    Three rules from plan 21 §5, and each one matters:
      - The main feels nothing back. Real players hold no regard rows, so this only ever writes alt -> someone.
      - Existing rows are kept. The seed fills a `description` only where it is empty and never touches an
        existing `score`, so a feeling a bot actually earned is never overwritten by a story -- and re-running
        it cannot restore a boost that has since been spent.
      - It is a boost, not a floor. `baseline` is left to regard.py: writing the warmth there too made the
        head start permanent, because decay() pulls every score back toward baseline (see the INSERT below).
      - Between alts the warmth is halved, and no description is written: the bond's words are about the main,
        not about each other. regard.py's `describe` gives them one once they have actually met.

    This writes regard.py's table on purpose -- plan 21 §5 puts the seed in either service, and the bond lives here.
    """
    where = f"la.era = {q(e['name'])}"
    if guids:
        where += " AND la.guid IN (" + ",".join(str(int(g)) for g in guids) + ")"
    rows = [dict(guid=int(r[0]), main=int(r[1]), kind=r[2], bond="\t".join(r[3:]))
            for r in sql(f"SELECT la.guid, la.main_guid, la.kind, {_flat('la.bond')} FROM lore_alt la "
                         f"WHERE {where} ORDER BY la.guid")]
    written = 0
    for a in rows:
        warmth = BOND_REGARD.get(a["kind"])
        if warmth is None:
            log(f"guid {a['guid']}: bond kind {a['kind']!r} has no warmth in BOND_REGARD; skipped")
            continue
        ties = [(a["main"], float(warmth), first_sentence(a["bond"]))]
        ties += [(b["guid"], warmth / 2.0, "") for b in rows
                 if b["guid"] != a["guid"] and b["main"] == a["main"]]
        for about, score, desc in ties:
            if dry_run:
                log(f"  {a['guid']} -> {about}: {score:.0f} ({a['kind']})"
                    + (f" \"{desc[:60]}\"" if desc else ""))
                written += 1
                continue
            # `baseline` is deliberately NOT written: it is regard.py's to compute (same company 20, other
            # faction -15, otherwise 0), and decay() pulls every score back toward it. Writing the bond
            # warmth there made the head start a permanent FLOOR -- Grommell sat at exactly 60.0 toward his
            # cousin for six days, unable to drift, because score and baseline were the same number. A bond
            # is a starting boost, not a standing debt: the alt walks up already knowing them, and from
            # there feels whatever the same rules give everyone else, including dislike.
            #
            # On a row that already exists, the score is left alone entirely. Re-running this must not
            # restore a boost that has since been spent -- an idempotent seed cannot also be a top-up.
            sql("INSERT INTO regard (bot_guid, other_guid, score, familiarity, description) VALUES "
                f"({a['guid']}, {about}, {score:.2f}, {BOND_FAMILIARITY}, "
                + (q(desc) if desc else "NULL") + ") "
                "ON DUPLICATE KEY UPDATE familiarity = GREATEST(familiarity, VALUES(familiarity)), "
                "description = COALESCE(NULLIF(description, ''), VALUES(description)), "
                "updated_at = NOW()", fetch=False)
            written += 1
    return written


def bond_regard(args):
    e = eras.get(args.era)
    guids = None
    if args.names:
        names = [n.strip() for n in args.names.split(",") if n.strip()]
        guids = [int(r[0]) for r in sql("SELECT guid FROM characters WHERE name IN ("
                                        + ", ".join(q(n) for n in names) + ")")]
        if not guids:
            sys.exit("no characters by those names")
    n = seed_bond_regard(e, guids, dry_run=args.dry_run)
    log(f"{n} regard rows {'would be ' if args.dry_run else ''}seeded from bonds")
    return 0


def alts(args):
    e = eras.get(args.era)
    ensure_schema()
    names = [n.strip() for n in args.names.split(",") if n.strip()] if args.names else None
    rows = alt_rows(e, names, getattr(args, "account", 0))
    if not rows:
        sys.exit("no alts found: is there a player_main row for the account?")
    lane = fleet.pick_lanes(args.lanes)[0]
    judge = None if args.no_judge else fleet.Judge()
    tempers = temperaments()
    rng = random.Random(args.seed)
    written = 0

    for c in rows:
        m = main_of(c["guid"])
        if not m:
            log(f"{c['name']}: no main; skipped")
            continue
        path = os.path.join(ALTS_DIR, c["name"].lower() + ".txt")
        concept = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        keep = [k.strip() for k in args.keep.split(",") if k.strip()]
        keep_story = [k.strip() for k in args.keep_story.split(",") if k.strip()]
        have = sql(f"SELECT kind, pinned, bond FROM lore_alt WHERE guid = {c['guid']}")
        if have and not args.rebond:
            log(f"{c['name']}: already bound to {m['name']} as {have[0][0]}; use --rebond to write it again")
        else:
            allowed = allowed_kinds(c, m)
            if args.kind:
                if args.kind not in allowed:
                    sys.exit(f"{c['name']} cannot be {args.kind} to {m['name']}: allowed are {', '.join(allowed)}")
                kind, pinned = args.kind, 1
            elif have and int(have[0][1]):
                kind, pinned = have[0][0], 1
            else:
                kind = rng.choices(allowed, weights=[BOND_KINDS[k]["weight"] for k in allowed])[0]
                pinned = 0
            other_rows = sql("SELECT c2.name, la.kind, la.bond FROM lore_alt la "
                             "JOIN characters c2 ON c2.guid = la.guid "
                             f"WHERE la.main_guid = {m['guid']} AND la.guid <> {c['guid']}")
            others = [(r[0], r[1]) for r in other_rows]
            other_bonds = [r[2] for r in other_rows]
            log(f"{c['name']}: {kind} to {m['name']}"
                + (" (chosen)" if pinned else " (drawn)") + (", concept given" if concept else ""))
            bond = None
            for attempt in range(1, args.attempts + 1):
                try:
                    bond = build_bond(lane, c, m, kind, concept, others, e, keep)
                    check_bond_extra(bond, kind, keep, other_bonds)
                    vet(bond, e, judge, attempt, f"bond {c['guid']} {c['name']}")
                    break
                except ValueError as err:
                    log(f"  bond attempt {attempt}: {err}")
                    bond = None
            if not bond:
                log(f"{c['name']}: no bond passed the checks; skipped")
                continue
            print(f"\n--- {c['name']} -> {m['name']} ({kind}), {len(bond.split())} words ---\n{bond}\n")
            if not args.dry_run:
                sql(f"INSERT INTO lore_alt (guid, main_guid, kind, pinned, bond, sheet_hash, model, era) VALUES "
                    f"({c['guid']}, {m['guid']}, {q(kind)}, {pinned}, {q(bond)}, {q(m['sheet_hash'])}, "
                    f"{q((lane.name + ':' + lane.model)[:64])}, {q(e['name'])}) "
                    f"ON DUPLICATE KEY UPDATE main_guid = VALUES(main_guid), kind = VALUES(kind), "
                    f"pinned = VALUES(pinned), bond = VALUES(bond), sheet_hash = VALUES(sheet_hash), "
                    f"model = VALUES(model), era = VALUES(era)", fetch=False)

        bond_row = sql(f"SELECT bond FROM lore_alt WHERE guid = {c['guid']}")
        bond = bond_row[0][0] if bond_row else None
        if not bond:
            continue
        if sql(f"SELECT 1 FROM lore_character WHERE guid = {c['guid']}") and not args.rewrite:
            log(f"{c['name']}: already has a story; use --rewrite to write it again")
            continue
        # A drawn temperament can contradict the player's own words -- a first run gave both of these characters
        # HOTHEAD, so a light-hearted cousin "spoke with his fists before his tongue". It drives the dialogue
        # templates, so when the player has said who someone is, pin it.
        if args.temperament:
            temper = next((t for t in tempers if t[0].upper() == args.temperament.upper()), None)
            if not temper:
                sys.exit(f"no temperament named {args.temperament}; try one of: "
                         + ", ".join(sorted(t[0] for t in tempers)))
        else:
            temper = rng.choice(tempers)
        text = None
        for attempt in range(1, args.attempts + 1):
            try:
                if args.dry_run:
                    text = check_backstory(lane.chat([{"role": "user", "content": char_prompt(
                        c, temper, None, e, bond=bond, main_name=m["name"], concept=concept,
                        main_sheet=m["sheet"], keep=keep_story)}], 600), ALT_STORY_WORDS)
                else:
                    text = gen_alt_backstory(lane, attempt, e, judge, c, temper, bond, m, concept, keep_story)
                break
            except ValueError as err:
                log(f"  story attempt {attempt}: {err}")
                text = None
        if not text:
            log(f"{c['name']}: no story passed the checks")
            continue
        written += 1
        print(f"--- {c['name']}, {len(text.split())} words, temperament {temper[0]} ---\n{text}\n")

    # Seed at bond time (plan 21 §5), so a bond never sits in lore_alt without the feeling it implies.
    if not args.dry_run:
        seeded = seed_bond_regard(e, [c["guid"] for c in rows])
        if seeded:
            log(f"regard: {seeded} rows seeded from bonds")

    log(f"done: {written} stories written"
        + ("; nothing stored (dry run)" if args.dry_run else "; now run `traits` to give them personality"))
    return 0


# ---------------------------------------------------------------------------
# the player's own main (plan 21 §2)

MAIN_SHEET_PROMPT = """{setting}

Write a short sheet about a living person in this world, for those who know them to read. It is not a story and
not a boast: it is what someone who had travelled with {name} would be able to tell you about them.

{name} is a {race} {cls} of the {faction}, {standing}.

Their own account of themselves is below. Keep every fact of it: who their kin are, where they came from and
what that place was like, what became of it and of their family, the work they did and why they left, how they
are with other people, and what they are trying to do now. Invent nothing that is not there. Give them no new
names, no deeds, no battles, and no ending to the things left unresolved.

Be especially careful with what they want. Keep their aim at its full size, in their own measure: do not shrink
it, do not have them deny it or call it no great plan, and do not hand them some other reason for it than the
one they give. If they mean to grow strong, to gather others to them, and to put down the evils of this world,
then say so plainly.

---
{source}
---

Write one paragraph of {lo} to {hi} words, in the third person, about them. Never address the reader: no "you"
and no "your". Plain speech: no grand words, no figures, no numbers, nothing of a later age. Do not begin two
sentences with their name.

Return only the paragraph.
"""

# What the check accepts, and what the prompt asks for. They are deliberately different: the models overshoot a
# stated maximum, so the ask is the narrower band.
#
# The width was paid for. This prompt asks for every fact of the player's account, their aim at full size, and no
# invented names, and that takes about 250 words to do. Asking 130-200 and checking 130-200 gave 213, 236, 212 and
# 208; checking 130-230 gave 245, 253, 241 and one that scraped in at 228, then 263, 272 and two that dropped a
# fact to fit. A cap below the length the content needs does not make the sheet tighter, it makes the model throw
# facts away. Plan 21 §2's original 50-150 was written before anyone had seen a player's actual source text.
MAIN_SHEET_WORDS = (130, 290)
MAIN_SHEET_TARGET = (200, 240)


def check_main_sheet(text, name, anchors, source="", race=""):
    text = re.sub(r"\s+", " ", text).strip().strip('"')
    words = len(text.split())
    lo, hi = MAIN_SHEET_WORDS
    if not lo <= words <= hi:
        raise ValueError(f"{words} words")
    if name.lower() not in text.lower():
        raise ValueError("does not name the character")
    if text.lower().startswith("you"):
        raise ValueError("second person")
    if re.search(r"\d", text):
        raise ValueError("carries a figure")
    missing = [a for a in anchors if a.lower() not in text.lower()]
    if missing:
        raise ValueError("dropped: " + ", ".join(missing))
    # Name no place and no person the player did not name. The models like to add a landmark or a second region
    # for colour -- a first run put an Ironforge dwarf in Durotar and had him staring at Blackrock Mountain.
    # Every alt's bond is written from this sheet, so an invented place would be inherited by all of them.
    if source:
        known = {w.lower() for w in proper_names(source)}
        known |= {name.lower(), race.lower(), "alliance", "horde"}
        new = sorted({w for w in proper_names(text) if w.lower() not in known})
        if new:
            raise ValueError("invented names: " + ", ".join(new))
    return text


def build_main_sheet(lane, c, e, source):
    prompt = MAIN_SHEET_PROMPT.format(
        setting=e["setting"], name=c["name"], race=RACES.get(c["race"], "unknown"),
        cls=CLASSES.get(c["cls"], "wanderer"), faction="Horde" if c["race"] in HORDE else "Alliance",
        standing=eras.standing(c["level"], e), source=source.strip(),
        lo=MAIN_SHEET_TARGET[0], hi=MAIN_SHEET_TARGET[1])
    raw = lane.chat([{"role": "user", "content": prompt}], 700, temperature=0.6)
    return re.sub(r"\s+", " ", str(raw or "")).translate(ASCII_PUNCT).strip().strip('"')


def main_sheet(args):
    import hashlib
    e = eras.get(args.era)
    ensure_schema()
    rows = sql("SELECT guid, name, race, class, level FROM characters WHERE name = " + q(args.name))
    if not rows:
        sys.exit(f"no character named {args.name}")
    c = dict(guid=int(rows[0][0]), name=rows[0][1], race=int(rows[0][2]), cls=int(rows[0][3]),
             level=int(rows[0][4]))
    # A sheet is the character an account calls its own, and every alt's bond is written from it. Selecting
    # by name alone would write one for a bot or for somebody else's alt (plan 43 H5).
    kind_rows = sql(f"SELECT kind FROM person_kind WHERE guid = {c['guid']}")
    kind = kind_rows[0][0] if kind_rows else "unknown"
    if kind != "main":
        sys.exit(f"{c['name']} is {kind}, not a named main. A sheet belongs to the character its account "
                 f"calls its own -- name them first (GUIDE 10.5).")
    source = open(args.source, encoding="utf-8").read()
    anchors = [a.strip() for a in args.keep.split(",") if a.strip()]
    lane = fleet.pick_lanes(args.lanes)[0]
    judge = None if args.no_judge else fleet.Judge()
    log(f"{c['name']} ({RACES.get(c['race'])} {CLASSES.get(c['cls'])}, {eras.standing(c['level'], e)}) "
        f"on {lane.name}; keeping: {', '.join(anchors) or 'nothing named'}")

    text = None
    # More tries than the fleet's default: these checks are strict on purpose, and a rejection is cheap.
    # Note that vet() stops raising once attempt reaches fleet.MAX_ATTEMPTS and records a judge flag in
    # review-<era>.jsonl instead, so a judge doubt past that point is written down rather than retried.
    for attempt in range(1, args.attempts + 1):
        try:
            text = check_main_sheet(build_main_sheet(lane, c, e, source), c["name"], anchors,
                                    source, RACES.get(c["race"], ""))
            vet(text, e, judge, attempt, f"main sheet {c['guid']} {c['name']}")
            break
        except ValueError as err:
            log(f"attempt {attempt}: {err}")
            text = None
    if not text:
        sys.exit("no sheet passed the checks")

    print(f"\n--- {c['name']}, {len(text.split())} words ---\n{text}\n")
    if args.dry_run:
        log("dry run: nothing stored")
        return 0
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
    sql(f"INSERT INTO lore_main (guid, sheet, source, sheet_hash, model, era) VALUES ({c['guid']}, {q(text)}, "
        f"{q(source.strip())}, '{digest}', {q(lane.model)}, {q(e['name'])}) "
        f"ON DUPLICATE KEY UPDATE sheet = VALUES(sheet), source = VALUES(source), "
        f"sheet_hash = VALUES(sheet_hash), model = VALUES(model), era = VALUES(era)", fetch=False)
    log(f"stored lore_main for {c['name']} (guid {c['guid']}), sheet_hash {digest[:12]}")
    return 0


def project(_args):
    # Lines added to a story after it was written (lore_character_note, kept by regard.py: e.g. the company a
    # character rode with broke apart) follow the text they amend.
    has_notes = int(sql("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() "
                        "AND table_name = 'lore_character_note'")[0][0])
    notes = ("COALESCE((SELECT CONCAT(' ', GROUP_CONCAT(n.note ORDER BY n.id SEPARATOR ' ')) "
             "FROM lore_character_note n WHERE n.guid = l.guid), '')") if has_notes else "''"
    # Plan 33. Each piece adds nothing at all until its column is filled, so project() stays safe to
    # run part-way through a crafts run: it never leaves a label with an empty tail behind it.
    def life(column, label):
        return f"IFNULL(CONCAT(' {label}: ', l.{column}), '')"

    craft_s, kin_s = life("craft_short", "Your trade"), life("kin_short", "Your people")
    craft_f, kin_f = life("craft", "Your trade"), life("kin", "Your people")
    sql("START TRANSACTION;"
        "DELETE FROM mod_ollama_chat_personality WHERE personality LIKE 'BIO\\_%';"
        "DELETE FROM mod_ollama_chat_personality_templates WHERE `key` LIKE 'BIO\\_%' OR `key` LIKE 'BIOX\\_%';"
        # Core tier, in every prompt: the temperament's voice, then personality, gist and the one-line
        # motivation once traits exist (plan 15); until then the backstory.
        #
        # The temperament leads. It used to be joined in ONLY where a personality was absent -- and every
        # one of the 1,283 characters has a personality, so all 33 temperaments were dead code that had
        # never reached a single prompt (plans/30 §2). That is what the whole cast sounding alike was: the
        # generated personalities carry the grim register of the world they were written in (58% of them use
        # a grim, silent or stoic word; 12% any word of warmth), and the one line that would have said "you
        # make light of danger with dry jokes" or "you are loud and friendly, quick to invite others along"
        # was being thrown away. It goes first because a disposition is read as a voice, and the paragraph
        # after it as history.
        "INSERT INTO mod_ollama_chat_personality_templates (`key`, prompt, manual_only) "
        "SELECT CONCAT('BIO_', l.guid), IF(l.personality IS NULL, CONCAT(t.prompt, ' ', l.backstory, " + notes + "), "
        "CONCAT(t.prompt, ' ', l.personality, ' ', l.gist, " + notes + ", " + craft_s + ", " + kin_s +
        ", ' What drives you: ', l.motivation_short)), 1 "
        "FROM lore_character l JOIN mod_ollama_chat_personality_templates t ON t.`key` = l.temperament;"
        # Full tier, when someone speaks to the bot (mod-ollama-chat local patch): the same voice, then
        # personality, the whole backstory and the whole motivation.
        "INSERT INTO mod_ollama_chat_personality_templates (`key`, prompt, manual_only) "
        "SELECT CONCAT('BIOX_', l.guid), CONCAT(t.prompt, ' ', l.personality, ' ', l.backstory, " + notes +
        ", " + craft_f + ", " + kin_f + ", ' What drives you: ', l.motivation), 1 "
        "FROM lore_character l JOIN mod_ollama_chat_personality_templates t ON t.`key` = l.temperament "
        "WHERE l.personality IS NOT NULL;"
        "REPLACE INTO mod_ollama_chat_personality (guid, personality) "
        "SELECT guid, CONCAT('BIO_', guid) FROM lore_character;"
        "UPDATE guild g JOIN lore_guild l ON l.guildid = g.guildid SET g.info = l.charter, g.motd = l.motto;"
        # A guild with no lore for the era (e.g. all members parked until a later expansion) must not
        # keep text projected for another era.
        "UPDATE guild g LEFT JOIN lore_guild l ON l.guildid = g.guildid SET g.info = '', g.motd = '' "
        "WHERE l.guildid IS NULL;"
        "COMMIT;", fetch=False)
    n = sql("SELECT COUNT(*) FROM mod_ollama_chat_personality WHERE personality LIKE 'BIO\\_%'")[0][0]
    full = sql("SELECT COUNT(*) FROM mod_ollama_chat_personality_templates WHERE `key` LIKE 'BIOX\\_%'")[0][0]
    g = sql("SELECT COUNT(*) FROM lore_guild")[0][0]
    lives = sql("SELECT COUNT(*) FROM lore_character WHERE craft IS NOT NULL")[0][0]

    # The dashboard's detail panel reads who each character is from here.
    lore = {}
    for r in sql("SELECT l.guid, COALESCE(l.motivation_kind, ''), "
                 f"{_flat(TARGET_NAME_SQL)}, {_flat('l.personality')}, {_flat('l.motivation_short')}, "
                 f"{_flat('l.gist')}, {_flat('l.craft_short')}, {_flat('l.kin_short')} "
                 "FROM lore_character l WHERE l.personality IS NOT NULL"):
        lore[r[0]] = dict(kind=r[1], target=r[2], personality=r[3], motivation_short=r[4], gist=r[5],
                          craft=r[6], kin=r[7])
    os.makedirs(os.path.dirname(DASHBOARD_LORE), exist_ok=True)
    with open(DASHBOARD_LORE + ".tmp", "w") as f:
        json.dump(lore, f, separators=(",", ":"))
    os.replace(DASHBOARD_LORE + ".tmp", DASHBOARD_LORE)
    log(f"projected {n} characters ({full} with the full conversation tier, {lives} with a working "
        f"life) and {g} guild charters; "
        f"{len(lore)} in the dashboard's lore.json")
    return 0


def era_scan(args):
    """Later-age names in stored texts (plan 12; the guide's Phase 10 check).

    The era's own META pattern -- the one generation rejects on -- applied to what is already in the
    database: texts written under an earlier era, hand-edited since, or let through before a name was
    added to the pattern. Reports only; nothing is rewritten. Exit 1 when anything matched, so a phase
    check can depend on it.
    """
    e = eras.get(args.era)
    # table, key column, what one row is, and every column a model wrote into it.
    targets = [
        ("lore_character", "guid", "character", ("backstory", "personality", "gist", "motivation",
                                                 "motivation_short", "craft", "craft_short", "kin", "kin_short")),
        ("lore_guild", "guildid", "company", ("history", "charter", "motto")),
        ("lore_alt", "guid", "alt bond", ("bond",)),
        ("lore_main", "guid", "main sheet", ("sheet",)),
        ("chronicle_entry", "id", "chronicle entry", ("title", "body")),
        ("chronicle_personal", "id", "household chronicle", ("title", "body")),
        ("chronicle_tale", "id", "tale", ("words",)),
        ("chronicle_rumour", "id", "rumour", ("words",)),
    ]
    have = {}
    for row in sql("SELECT table_name, column_name FROM information_schema.columns "
                   "WHERE table_schema = DATABASE()"):
        have.setdefault(row[0], set()).add(row[1])

    hits, terms, total = [], {}, 0
    for table, key, label, columns in targets:
        if table not in have:
            continue
        # Only the era's own rows: a Classic scan must not flag a story written for wotlk.
        where_era = f" AND era = {q(e['name'])}" if "era" in have[table] else ""
        for column in columns:
            if column not in have[table]:
                continue
            rows = sql(f"SELECT {key}, {column} FROM {table} "
                       f"WHERE {column} IS NOT NULL AND {column} <> ''{where_era}")
            for r in rows:
                if len(r) < 2 or r[1] == "NULL":
                    continue
                total += 1
                found = sorted({m.group(0).lower() for m in e["meta"].finditer(r[1])})
                if found:
                    hits.append((label, table, column, r[0], found, r[1]))
                    for t in found:
                        terms[t] = terms.get(t, 0) + 1

    print(f"era {e['name']}: scanned {total} stored texts in {len(have)} tables")
    if not hits:
        print("no later-age names found")
        return 0
    print(f"{len(hits)} texts carry a later-age name\n")
    by_label = {}
    for label, table, column, _key, _found, _text in hits:
        by_label[(label, table, column)] = by_label.get((label, table, column), 0) + 1
    for (label, table, column), n in sorted(by_label.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5}  {label:20} {table}.{column}")
    print("\nnames found:")
    for t, n in sorted(terms.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5}  {t}")
    print(f"\nfirst {min(args.show, len(hits))}:")
    for label, table, column, key, found, text in hits[:args.show]:
        where = text.lower().find(found[0])
        print(f"  {table}.{column} {key} [{', '.join(found)}]")
        print(f"    ...{text[max(0, where - 60):where + 90]}...")
    print("\nCompany histories and hand-written texts are fixed by hand; generated character texts can be\n"
          "rewritten with `restand`, or regenerated for that character.")
    return 1


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("generate", "sample"):
        p = sub.add_parser(name)
        p.add_argument("--era", required=True)
        p.add_argument("--lanes", default=fleet.DEFAULT_LANES)
        p.add_argument("--slots", default="", help="per-lane slot overrides, e.g. z13-qwen35=2,zb-qwen80=0")
    g = sub.choices["generate"]
    g.add_argument("--unguilded", action="store_true")
    g.add_argument("--limit", type=int, default=0)
    g.add_argument("--no-judge", action="store_true")
    t = sub.add_parser("traits", help="personality and main motivation for characters that lack them (plan 15)")
    t.add_argument("--era", required=True)
    t.add_argument("--lanes", default=fleet.DEFAULT_LANES)
    t.add_argument("--slots", default="")
    t.add_argument("--limit", type=int, default=0)
    t.add_argument("--no-judge", action="store_true")
    cf_ = sub.add_parser("crafts", help="a trade and a household for characters that lack one (plan 33)")
    cf_.add_argument("--era", required=True)
    cf_.add_argument("--lanes", default=fleet.DEFAULT_LANES)
    cf_.add_argument("--slots", default="")
    cf_.add_argument("--limit", type=int, default=0)
    cf_.add_argument("--rewrite", action="store_true", help="write again for characters that already have one")
    cf_.add_argument("--no-judge", action="store_true")
    sc = sub.add_parser("sample-crafts", help="print a working life for random characters per lane; stores nothing")
    sc.add_argument("--era", required=True)
    sc.add_argument("--lanes", default=fleet.DEFAULT_LANES)
    sc.add_argument("--slots", default="")
    sc.add_argument("--per-lane", type=int, default=1)
    st = sub.add_parser("sample-traits", help="print traits for random characters on each lane; stores nothing")
    st.add_argument("--era", required=True)
    st.add_argument("--lanes", default=fleet.DEFAULT_LANES)
    st.add_argument("--slots", default="")
    st.add_argument("--per-lane", type=int, default=1)
    rc = sub.add_parser("reconcile", help="rewrite personalities that contradict their own temperament")
    rc.add_argument("--era", required=True)
    rc.add_argument("--lanes", default="evo-quality,z13-qwen35")
    rc.add_argument("--slots", default="")
    rc.add_argument("--dry-run", action="store_true")
    sf = sub.add_parser("soften", help="rewrite grim personalities written under a warm temperament (plan 37)")
    sf.add_argument("--era", required=True)
    sf.add_argument("--lanes", default="evo-quality,z13-qwen35")
    sf.add_argument("--slots", default="")
    sf.add_argument("--limit", type=int, default=0, help="rewrite at most this many")
    sf.add_argument("--show", type=int, default=12, help="how many to print on a dry run")
    sf.add_argument("--dry-run", action="store_true", help="store nothing; print what would be rewritten")
    vp = sub.add_parser("vary-personality", help="rewrite personality lines that share a common opening")
    vp.add_argument("--era", required=True)
    vp.add_argument("--lanes", default="evo-quality,z13-qwen35")
    vp.add_argument("--slots", default="")
    vp.add_argument("--min-repeats", type=int, default=8)
    vp.add_argument("--keep", type=int, default=3)
    vt = sub.add_parser("vary-traits", help="rewrite gist / motivation_short lines that share a common opening")
    vt.add_argument("--era", required=True)
    vt.add_argument("--fields", default="gist,motivation_short",
                    help="comma-separated: " + ", ".join(VARY_FIELDS))
    vt.add_argument("--lanes", default="evo-quality,z13-qwen35")
    vt.add_argument("--slots", default="")
    vt.add_argument("--words", type=int, default=2, help="opening length compared, in words")
    vt.add_argument("--min-repeats", type=int, default=8)
    vt.add_argument("--keep", type=int, default=3)
    vt.add_argument("--limit", type=int, default=0, help="rewrite at most this many lines per field")
    vt.add_argument("--show", type=int, default=8)
    vt.add_argument("--dry-run", action="store_true", help="store nothing; print what would be rewritten")
    npool = sub.add_parser("name-pools", help="given names per culture and gender for rename-overused")
    npool.add_argument("--era", required=True)
    npool.add_argument("--per-pool", type=int, default=60)
    npool.add_argument("--cultures", default="", help="only these cultures' pools, e.g. Troll,Tauren; the rest are kept")
    rn = sub.add_parser("rename-overused", help="give each character its own name for overused invented people")
    rn.add_argument("--era", required=True)
    rn.add_argument("--dry-run", action="store_true")
    rn.add_argument("--show", type=int, default=12)
    rn.add_argument("--seed", type=int, default=15)
    rp = sub.add_parser("repool", help="rename invented people named from a replaced name pool")
    rp.add_argument("--era", required=True)
    rp.add_argument("--old", required=True, help="the names.json the replaced names came from")
    rp.add_argument("--cultures", required=True)
    rp.add_argument("--dry-run", action="store_true")
    rp.add_argument("--show", type=int, default=12)
    rp.add_argument("--seed", type=int, default=15)
    rs = sub.add_parser("restand", help="fit stories to where re-rolled characters stand now")
    rs.add_argument("--era", required=True)
    rs.add_argument("--lanes", default="evo-quality,z13-qwen35")
    rs.add_argument("--slots", default="")
    rs.add_argument("--gap", type=int, default=2, help="bands between written and current standing before a revision")
    rs.add_argument("--limit", type=int, default=0, help="a random sample of this many characters")
    rs.add_argument("--seed", type=int, default=15)
    rs.add_argument("--show", type=int, default=8)
    rs.add_argument("--check-only", action="store_true", help="judge and stamp fitting stories; revise nothing")
    rs.add_argument("--dry-run", action="store_true", help="store nothing; print verdicts and up to --show revisions")
    rs.add_argument("--no-judge", action="store_true")
    tr = sub.add_parser("traits-report", help="variety of kinds, targets and personality openings")
    tr.add_argument("--era", required=True)
    al = sub.add_parser("alts", help="bond and backstory for a player's alts, bound to their main (plan 21 §3-§4)")
    al.add_argument("--era", required=True)
    al.add_argument("--names", default="", help="comma-separated character names; default every alt in the era")
    al.add_argument("--account", type=int, default=0,
                    help="only this household's alts; default every alt in the era, across all accounts")
    al.add_argument("--kind", default="", help="pin the bond kind: " + ", ".join(BOND_KINDS))
    al.add_argument("--keep", default="",
                    help="comma-separated words the BOND must carry (what is between them), e.g. cousin")
    al.add_argument("--keep-story", default="",
                    help="comma-separated words the BACKSTORY must carry (who they are), e.g. wealth")
    al.add_argument("--temperament", default="",
                    help="pin the personality template (e.g. WANDERER, SCHEMER); default draws one at random")
    al.add_argument("--lanes", default="evo-quality")
    al.add_argument("--attempts", type=int, default=6)
    al.add_argument("--seed", type=int, default=15)
    al.add_argument("--rebond", action="store_true", help="write the bond again even if one exists")
    al.add_argument("--rewrite", action="store_true", help="write the backstory again even if one exists")
    al.add_argument("--dry-run", action="store_true", help="print the bond and story and store nothing")
    al.add_argument("--no-judge", action="store_true")
    br = sub.add_parser("bond-regard",
                        help="seed regard from bonds already written: alt -> main, and half between alts (plan 21 P5)")
    br.add_argument("--era", required=True)
    br.add_argument("--names", default="", help="comma-separated alt names; default every bond in the era")
    br.add_argument("--dry-run", action="store_true", help="print what would be written and store nothing")
    ms = sub.add_parser("main-sheet",
                        help="a player's own account of their main, tidied into one in-world paragraph (plan 21 §2)")
    ms.add_argument("--era", required=True)
    ms.add_argument("--name", required=True, help="the character's name: the one this account calls its own")
    ms.add_argument("--source", required=True, help="a text file of the player's own words")
    ms.add_argument("--keep", default="",
                    help="comma-separated words the sheet must still carry, e.g. Ironforge,Menethil,Dark Iron")
    ms.add_argument("--lanes", default="evo-quality")
    ms.add_argument("--attempts", type=int, default=6, help="tries before giving up; the checks are strict")
    ms.add_argument("--dry-run", action="store_true", help="print the sheet and store nothing")
    ms.add_argument("--no-judge", action="store_true")
    r = sub.add_parser("reset")
    r.add_argument("--backup-dir", required=True)
    es = sub.add_parser("era-scan", help="later-age names in stored texts (reports only; exit 1 on a hit)")
    es.add_argument("--era", required=True)
    es.add_argument("--show", type=int, default=15, help="how many matching texts to print")
    sub.add_parser("project")
    args = ap.parse_args()
    sys.exit({"generate": generate, "sample": sample, "reset": reset, "project": project, "traits": traits,
              "sample-traits": sample_traits, "traits-report": traits_report,
              "crafts": crafts, "sample-crafts": sample_crafts,
              "reconcile": reconcile, "soften": soften,
              "vary-personality": vary_personality, "vary-traits": vary_traits, "name-pools": name_pools,
              "rename-overused": rename_overused, "repool": repool, "restand": restand,
              "main-sheet": main_sheet, "alts": alts, "bond-regard": bond_regard,
              "era-scan": era_scan}[args.cmd](args))


if __name__ == "__main__":
    main()
