#!/usr/bin/env python3
"""gen_places.py - the almanac of places (custom wow plans/46, plan 40 S3).

One short fragment per zone per angle, saying what a place is actually like, so a companion walking into
Duskwood sounds different from one walking into Mulgore.

**Nothing about Azeroth is written into this repository.** The zone list comes from the realm's own
`ledger_event` kills, the names from the client's own `AreaTable.dbc` (via services/regard/areas.py, which
already follows this rule), and every generated sentence goes into `place_words` in the database. A realm
that has never been played produces no almanac, and two realms produce two different ones.

    python3 gen_places.py sample   --era classic [--zones 1,17]   # prints, stores nothing
    python3 gen_places.py generate --era classic [--zones ...] [--rewrite] [--dry-run]
    python3 gen_places.py collide  --era classic [--prune]        # cross-zone phrase collisions
    python3 gen_places.py report   --era classic                  # coverage
    python3 gen_places.py draft    --era classic --zones 1637 --save f.json   # no seed; stores nothing
    python3 gen_places.py keep     --era classic --save f.json    # store the draft exactly as read

Why the seed is the ledger and not the world database: `creature.zoneId` is 0 for 144,979 of 150,172 spawns
and `areaId` for 146,271 -- both are optional caches, and the ids that are set belong to later expansions.
Resolving spawns to zones needs the core's map and DBC machinery, not SQL. The ledger already knows what this
realm has actually met, weighted by how often, which is the better question anyway (plan 46 section 2).
"""

import argparse
import collections
import json
import os
import random
import re
import sys
import time

# realpath, not abspath: /opt/wow/lore is a symlink into the repo, and abspath does not follow one -- the
# same trap that took the lore gate down (plan 23 W2).
HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))          # services/, for common.site
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "regard"))

import areas                    # noqa: E402  the client's AreaTable.dbc, read off the box
import era as eras              # noqa: E402
import fleet                    # noqa: E402
import gen_backstories as g     # noqa: E402
from common import site         # noqa: E402

SCHEMA = os.path.join(HERE, "place_tables.sql")
WORLD_DB = site.get("DB_WORLD", "acore_world")

# Four angles, not four paraphrases (plan 40 section 6 mandate 1). A cached description is static text handed
# identically to every bot standing in the zone, which is the shape that made one register out of the cast.
ANGLES = {
    "sight": "one thing a traveller sees here and nowhere else - a creature at its business, a shape in the "
             "land, a colour - shown plainly",
    "danger": "what actually kills people here, stated flatly as a fact about the place",
    "people": "who lives or works here, and what they are like to deal with",
    "grievance": "what someone who lives here complains about",
}
ORDER = ["sight", "danger", "people", "grievance"]

# plans/45. A "danger" angle invites the exact tic that plan owns -- "keep your eyes open", "watch the
# shadows". Shipping four of those per zone would install it in every zone at once, in the prompt itself,
# permanently. So it is forbidden in the prompt AND rejected here.
#
# Matched as a SHAPE, not as a word list. The first version matched those verbs anywhere and rejected
# "the leper gnomes keep me awake with their tinkering" -- a good grievance line, thrown away for using an
# ordinary verb. SECOND_PERSON already catches every "keep your ...", so what is left to catch is the bare
# imperative opening a sentence.
ADVICE = re.compile(r"(?:^|[.!?]\s+)(keep|hold|watch|mind|guard|stay|beware|trust|avoid|remember|do not|don't)\b",
                    re.I)
SECOND_PERSON = re.compile(r"\b(you|your|yours|yourself)\b", re.I)
# The fragment is handed to every bot in the zone as "what this land is like, to those who know it". An "I"
# or "my" in it has no owner, and a bot will take it for its own (plans/50: text in the prompt, spoken aloud).
# The regenerated Dun Morogh grievance opened "My lungs burn".
# Not "mine": Gnomeregan's land mines failed all four tries on it.
FIRST_PERSON = re.compile(r"\bI\b|\b(?i:me|my|myself|we|us|our|ours)\b")
# plans/45's "the X remembers": 11 of the first 176 fragments carried it. The sight angle's first brief ("the
# light, the ground, the sound, the smell of it") opened 27 of 44 zones on "The air", 13 on "the air smells of".
TICS = re.compile(r"\bremembers?\b|\bremembered\b", re.I)
STOCK_OPENING = re.compile(r"^(?:the\s+)?(?:air|ground|wind|sky|land|earth|light)\b", re.I)
MARKUP = re.compile(r'["“”*_#`\[\]]')

PROMPT = """You are a loremaster for {setting}
Everything you write is real life to the people in it. Never mention games, players, levels as numbers, specs, raids, loot, servers or anything outside the world.{limits}

The place: {zone}.
The creatures this fragment is about: {creatures}.

Write ONE fragment about this place, from this angle: {angle}

Rules:
- At most 35 words, one or two sentences.
- A plain statement about the place, as someone who has been there would put it, in the third person: no "I", "my", "we" or "our". No greeting, no dialogue, no quotation marks.
- NEVER advice, NEVER a warning, NEVER an instruction to anyone. Do not address a reader at all: no "you", no "your".
- No numbers, no lists, no markdown.
- Name at least one of the creatures above, and no other beast or monster, so it could not be mistaken for anywhere else.
- Kinds of creature in lowercase, as common nouns ("crag boars", not "Crag Boars"); only a tribe's or people's own name takes a capital.
- Do NOT begin with the name of the place, and do not begin "The ... in {zone}", "The air" or "The ground". Begin with something particular to this place. The listener already knows where they stand; the name may appear later or not at all.

Write only the fragment."""


# For a place the ledger cannot seed: a capital, where nothing is killed but the odd duel. The model writes
# from what it knows of the place, and the operator reads every fragment before `keep` stores it -- the era
# checks prove a fragment is of its time, never that it is true of the place.
SEEDLESS = (PROMPT
            .replace("The creatures this fragment is about: {creatures}.\n", "")
            .replace("- Name at least one of the creatures above, and no other beast or monster, so it could not "
                     "be mistaken for anywhere else.",
                     "- Name at least one of these, which are the real districts and buildings of {zone}: "
                     "{districts}. Name no district or building that is not on that list.\n"
                     "- No beasts or monsters: a city is its people, its buildings and its trades."))
# The first capitals draft, read by the operator 2026-09-24: 6 of 16 rejected. Crag boars (Dun Morogh's) in three
# Thunder Bluff fragments, Orgrimmar's Valley of Wisdom in Undercity, Northrend's Warsong Hold in Orgrimmar,
# and immortality granted in Darnassus. The last two are era errors; the first two are the kind no check can see.
# The second draft still invented "Skyhorn Mesa" and "Spiritwalker's Terrace", so the prompt now carries the
# city's real districts (areas.subzones: AreaTable + WMOAreaTable) and the validator requires one of them.


def districts(e, zone_id):
    """The zone's real districts, deduplicated case-insensitively, later-age names removed."""
    out = {}
    for name in areas.subzones(zone_id):
        if e["meta"].search(name) or re.search(r"\d", name) or name.lower() in NOT_PLACES:
            continue
        best = out.get(name.lower())
        if best is None or miscased(name) < miscased(best):
            out[name.lower()] = name     # "Valley of Heroes" over "Valley Of Heroes", "Terrace" over "terrace"
    return sorted(out.values())


def miscased(name):
    """Words cased against title style: a long word in lowercase, or a short one (of, the) capitalised mid-name."""
    words = name.split()
    return sum((len(w) > 3) != w[0].isupper() for w in words[1:])


# Rows in the client's tables that name a fixture, not a place (plain English, not Azeroth data).
NOT_PLACES = {"bank", "elevator", "gnomes"}


def elsewhere_kinds(e, zone_id):
    """Multi-word creature kinds the ledger met in OTHER zones. "crag boar" is Dun Morogh's; a seedless Thunder
    Bluff named it in four fragments across two drafts. One-word kinds are left out: "dwarf" belongs to
    Ironforge as much as to the Dark Iron."""
    out = set()
    for zid, _zone, creatures in zone_rows(e):
        if zid != zone_id:
            out.update(k for k in kinds(creatures, limit=30) if " " in k and k not in INDIVIDUALS)
    return out

# ---------------------------------------------------------------------------
# the realm's own record of its places

def ensure_schema():
    # The whole file in one call, exactly as gen_backstories.ensure_schema does it. Splitting on ";" is the
    # obvious thing and it is wrong: a semicolon inside a COLUMN COMMENT cuts the statement in half.
    with open(SCHEMA, encoding="utf-8") as fh:
        g.sql(fh.read(), fetch=False)


def zone_rows(e, min_kinds=8):
    """(zone_id, name, [creature names]) for every zone this realm has actually fought in.

    Zones whose own name is out of era are skipped: a classic realm does not describe Netherstorm even if
    somebody wandered there.
    """
    names = areas.load()
    rows = g.sql("SELECT zone_id, COUNT(*) AS kills, "
                 "COUNT(DISTINCT SUBSTRING_INDEX(SUBSTRING_INDEX(detail,'\"name\":\"',-1),'\"',1)) AS kinds "
                 "FROM ledger_event WHERE event_type = 'kill' AND zone_id > 0 "
                 f"GROUP BY zone_id HAVING kinds >= {int(min_kinds)} ORDER BY kills DESC")
    out = []
    for zid, _kills, _kinds in rows:
        zid = int(zid)
        name = names.get(zid)
        if not name:
            continue                      # no name on the box: nothing worth describing
        if e["meta"].search(name):
            continue                      # a later age's place
        out.append((zid, name, inhabitants(zid)))
    return out


# Age and size words, stripped so "Elder Crag Boar", "Large Crag Boar" and "Crag Boar" are one kind. Plain
# English, not Azeroth data. The first run handed all three to every angle, and three of Dun Morogh's four
# fragments came back about the same boars and snow leopards.
MODIFIERS = {"young", "elder", "juvenile", "large", "small", "ragged", "old", "adult", "mature", "giant",
             "greater", "lesser", "mangy", "starving", "diseased", "rabid", "feral", "whelp", "cub", "pup", "hatchling", "burly"}


def kinds(creatures, limit=8):
    """Collapse ledger names into distinct kinds, most-met first, lowercased for the prompt, modifiers
    stripped, one per head noun. Merging on ANY shared word was tried and collapsed a whole dungeon: every
    name in the Stockade begins "Defias". A named individual keeps its name and its capitals."""
    out, seen = [], set()
    for name in creatures:
        if name in INDIVIDUALS:
            key, shown = name.lower(), name
        else:
            # never the last word: "Searing Whelp" is a whelp, not a "searing"
            *front, head = name.lower().split()
            words = [w for w in front if w not in MODIFIERS] + [head]
            key, shown = head, " ".join(words)
        if key in seen:
            continue
        seen.add(key)
        out.append(shown)
        if len(out) == limit:
            break
    return out


def deal(kind_list, angle):
    """This angle's share of the kinds, dealt round-robin so no two angles are about the same creature."""
    return kind_list[ORDER.index(angle)::len(ORDER)]


def plural(word):
    """wolf/wolves, but mastiff/mastiffs."""
    if word.endswith("f") and not word.endswith("ff"):
        return re.escape(word[:-1]) + "(?:fs?|ves)"
    return re.escape(word) + "(?:s|es)?"


def names_kind(text, kind):
    """Does the text name this kind? Matched on its head noun, singular or plural."""
    return re.search(rf"\b{plural(kind.split()[-1])}\b", text, re.I) is not None


def without(text, kinds_):
    """The text with these kinds' full names blanked out, and their last two words, so that naming one's own
    "black ravager mastiff" is not read as naming another angle's "black ravager"."""
    for k in kinds_:
        w = k.split()
        for phrase in sorted({tuple(w), tuple(w[-2:])}, key=len, reverse=True):
            pat = r"\s+".join(map(re.escape, phrase[:-1])) + (r"\s+" if len(phrase) > 1 else "") + plural(phrase[-1])
            text = re.sub(rf"\b{pat}\b", " ", text, flags=re.I)
    return text


# Names the world has only one of: Bazil Thredd, Hamhock, Targorr the Dread. Filled by inhabitants(), read by
# kinds(). A person keeps their capitals and is never merged into a kind.
INDIVIDUALS = set()


def inhabitants(zone_id, limit=30):
    """Names met in this zone, most-killed first, with kinds of creature ahead of named individuals.

    An individual is an entry with exactly one spawn in the world database. That count is reliable where the
    spawn's zoneId is not (see the module docstring): it asks how many, not where."""
    rows = g.sql("SELECT SUBSTRING_INDEX(SUBSTRING_INDEX(detail,'\"name\":\"',-1),'\"',1) AS who, "
                 "MIN(CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(detail,'\"entry\":',-1),',',1) AS UNSIGNED)) AS entry, "
                 "COUNT(*) AS n FROM ledger_event "
                 f"WHERE event_type = 'kill' AND zone_id = {int(zone_id)} "
                 f"GROUP BY who ORDER BY n DESC LIMIT {int(limit)}")
    rows = [(r[0], int(r[1] or 0)) for r in rows if r[0]]
    entries = ",".join(str(e) for _, e in rows if e) or "0"
    single = {int(e) for (e,) in g.sql(f"SELECT id FROM {WORLD_DB}.creature WHERE id IN ({entries}) "
                                       "GROUP BY id HAVING COUNT(*) = 1")}
    people = [who for who, e in rows if e in single]
    INDIVIDUALS.update(people)
    return [who for who, e in rows if e not in single] + people


# ---------------------------------------------------------------------------
# generation

def build(lane, e, zone, creatures, angle):
    prompt = PROMPT.format(setting=e["setting"], limits=e["limits"], zone=zone,
                           creatures=", ".join(deal(kinds(creatures), angle)) or "little worth naming",
                           angle=ANGLES[angle])
    text = lane.chat([{"role": "user", "content": prompt}], 200, temperature=0.85)
    return clean(text)


def clean(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    text = text.strip().strip('"').strip()
    return " ".join(text.split())


def problems(text, e, zone, creatures, angle, judge=None):
    out = []
    if not text:
        return ["empty"]
    words = re.findall(r"[A-Za-z0-9']+", text)
    if len(text) > 255:
        out.append(f"{len(text)} chars over 255")
    if len(words) > 45:
        out.append(f"{len(words)} words over 45")
    if re.search(r"\d", text):
        out.append("contains a number")
    if MARKUP.search(text):
        out.append("quotes or markdown")
    if SECOND_PERSON.search(text):
        out.append("addresses the reader")
    if TICS.search(text):
        out.append("\"remembers\" (plans/45)")
    if STOCK_OPENING.search(text):
        out.append("stock opening: the air, the ground, the land")
    if FIRST_PERSON.search(text):
        out.append("first person: the fragment has no speaker")
    if ADVICE.search(text):
        out.append("advice or instruction (plans/45)")
    m = e["meta"].search(text)
    if m:
        out.append(f"out of era: {m.group(0)!r}")
    if zone.lower() in " ".join(words[:5]).lower():
        out.append("opens with the place's name")
    every = kinds(creatures)
    mine = deal(every, angle)
    if mine and not any(names_kind(text, k) for k in mine):
        out.append(f"not grounded: names none of {mine}")
    rest = without(text, mine)
    theirs = [k for k in every if k not in mine and names_kind(rest, k)]
    if theirs:
        out.append(f"names another angle's creature: {theirs}")
    if not out and judge is not None:
        evidence = judge.check(eras.judge_prompt(e, text, known=creatures))
        if evidence:
            out.append(f"judge: {evidence}")
    return out


def one(lane, attempt, e, judge, zid, zone, creatures, angle, variant, store=True):
    """Generate, validate and (optionally) store a single fragment. Raises on failure so the pool retries."""
    text = build(lane, e, zone, creatures, angle)
    bad = problems(text, e, zone, creatures, angle, judge)
    if bad:
        raise ValueError("; ".join(bad) + f" :: {text[:90]}")
    if store:
        g.sql("INSERT INTO place_words (zone_id, era, variant, angle, words) VALUES "
              f"({int(zid)}, {g.q(e['name'])}, {int(variant)}, {g.q(angle)}, {g.q(text)}) "
              "ON DUPLICATE KEY UPDATE angle = VALUES(angle), words = VALUES(words), updated_at = NOW()",
              fetch=False)
    return text


def lanes_for(args):
    chosen = [l for l in fleet.pick_lanes(args.lanes, args.slots) if "character" in l.kinds]
    if not chosen:
        sys.exit("no batch lane carries the 'character' kind; check site/fleet.toml")
    return chosen


def wanted_zones(e, args):
    rows = zone_rows(e, args.min_kinds)
    if args.zones:
        keep = {int(z) for z in args.zones.split(",") if z.strip()}
        rows = [r for r in rows if r[0] in keep]
        missing = keep - {r[0] for r in rows}
        if missing:
            g.log(f"no usable ledger history for zone(s): {sorted(missing)}")
    if args.limit:
        rows = rows[:args.limit]
    return rows


# ---------------------------------------------------------------------------
# commands

def generate(args):
    e = eras.get(args.era)
    ensure_schema()
    lanes = lanes_for(args)
    judge = None if args.no_judge else fleet.Judge()
    rows = wanted_zones(e, args)
    if not rows:
        sys.exit("no zones to describe: has this realm been played?")

    have = {(int(z), int(v)) for z, v in
            g.sql(f"SELECT zone_id, variant FROM place_words WHERE era = {g.q(e['name'])}")}
    jobs = []
    for zid, zone, creatures in rows:
        for variant, angle in enumerate(ORDER[:args.variants]):
            if not args.rewrite and (zid, variant) in have:
                continue
            jobs.append((zid, zone, creatures, angle, variant))

    g.log(f"era {e['name']}; {len(rows)} zones, {len(jobs)} fragments to write "
          f"({len(have)} already held); lanes: " + ", ".join(f"{l.name}x{l.slots}" for l in lanes))
    if args.dry_run:
        for zid, zone, creatures, angle, variant in jobs:
            g.log(f"  would write {zone} ({zid}) v{variant} [{angle}] from: {', '.join(creatures[:4])}")
        return 0

    pool = fleet.Pool(lanes, log=g.log)
    for zid, zone, creatures, angle, variant in jobs:
        pool.submit("character", 1,
                    lambda lane, attempt, a=(zid, zone, creatures, angle, variant):
                        one(lane, attempt, e, judge, *a),
                    f"place {a_label(zid, zone, angle)}")
    failures = pool.run()
    for j in failures:
        g.log(f"FAILED {j['label']}: {' | '.join(j['errors'])}")
    held = g.sql(f"SELECT COUNT(*) FROM place_words WHERE era = {g.q(e['name'])}")[0][0]
    g.log(f"done: {held} fragments held for era {e['name']}; {len(failures)} failed")
    g.log("now run `collide` before switching the prompt section on (plan 46 section 4)")
    return 1 if failures else 0


def draft(args):
    """Seedless fragments for named zones, checked like any other, written to --save for the operator to read.
    STORES NOTHING: `keep` stores exactly what was read, never a regeneration."""
    e = eras.get(args.era)
    if not args.zones or not args.save:
        sys.exit("draft needs --zones and --save")
    lanes = lanes_for(args)
    judge = None if args.no_judge else fleet.Judge()
    names = areas.load()
    out = []
    only = {(int(z), a) for z, a in (x.split(":") for x in args.only.split(",") if x.strip())}
    for zid in (int(z) for z in args.zones.split(",") if z.strip()):
        zone = names.get(zid)
        if not zone:
            sys.exit(f"zone {zid} has no name in AreaTable.dbc")
        real = districts(e, zid)
        foreign = elsewhere_kinds(e, zid)
        g.log(f"{zone}: districts {real}; {len(foreign)} creature kinds from other zones barred")
        for variant, angle in enumerate(ORDER[:args.variants]):
            if only and (zid, angle) not in only:
                continue
            for attempt in range(1, fleet.MAX_ATTEMPTS + 1):
                lane = lanes[(len(out) + attempt) % len(lanes)]
                text = clean(lane.chat([{"role": "user", "content": SEEDLESS.format(
                    setting=e["setting"], limits=e["limits"], zone=zone, angle=ANGLES[angle],
                    districts=", ".join(real) or zone)}],
                    200, temperature=0.85))
                bad = problems(text, e, zone, [], angle, judge)
                if real and not any(re.search(rf"\b{re.escape(d)}\b", text, re.I) for d in real):
                    bad.append("names none of the real districts")
                strays = [k for k in foreign if re.search(rf"\b{re.escape(k)}", text, re.I)]
                if strays:
                    bad.append(f"another zone's creature: {strays}")
                if not bad:
                    break
                g.log(f"  {zone} [{angle}] try {attempt} rejected: {'; '.join(bad)} :: {text[:80]}")
            else:
                g.log(f"FAILED {zone} [{angle}]")
                continue
            out.append({"zone_id": zid, "zone": zone, "variant": variant, "angle": angle, "words": text})
            print(f"{zone:14s} [{angle:9s}] {text}", flush=True)
    with open(args.save, "w", encoding="utf-8") as fh:
        json.dump({"era": e["name"], "fragments": out}, fh, indent=1, ensure_ascii=False)
    g.log(f"{len(out)} fragments in {args.save}; nothing stored. Read them, then `keep --save {args.save}`")
    return 0


def keep(args):
    """Store a draft file exactly as it was read. Rows can be dropped from the file first."""
    with open(args.save, encoding="utf-8") as fh:
        doc = json.load(fh)
    e = eras.get(args.era)
    if doc["era"] != e["name"]:
        sys.exit(f"draft is for era {doc['era']}, not {e['name']}")
    ensure_schema()
    for f in doc["fragments"]:
        g.sql("INSERT INTO place_words (zone_id, era, variant, angle, words) VALUES "
              f"({int(f['zone_id'])}, {g.q(e['name'])}, {int(f['variant'])}, {g.q(f['angle'])}, {g.q(f['words'])}) "
              "ON DUPLICATE KEY UPDATE angle = VALUES(angle), words = VALUES(words), updated_at = NOW()",
              fetch=False)
    g.log(f"stored {len(doc['fragments'])} fragments from {args.save}")
    return 0


def a_label(zid, zone, angle):
    return f"{zone} ({zid}) [{angle}]"


def sample(args):
    """Print fragments for a few zones on each lane; stores nothing."""
    e = eras.get(args.era)
    lanes = lanes_for(args)
    judge = None if args.no_judge else fleet.Judge()
    rows = wanted_zones(e, args) or []
    if not rows:
        sys.exit("no zones to describe: has this realm been played?")
    rng = random.Random(args.seed)
    if not args.zones:
        rng.shuffle(rows)
        rows = rows[:args.per_lane * len(lanes)]

    for i, (zid, zone, creatures) in enumerate(rows):
        lane = lanes[i % len(lanes)]
        print(f"\n== {zone} ({zid})  lane {lane.name}")
        print(f"   kinds: {', '.join(kinds(creatures))}")
        for variant, angle in enumerate(ORDER[:args.variants]):
            t0 = time.time()
            try:
                text = one(lane, 0, e, judge, zid, zone, creatures, angle, variant, store=False)
                print(f"   [{angle:9s}] {text}   ({time.time() - t0:.0f}s)")
            except Exception as ex:
                print(f"   [{angle:9s}] REJECTED {ex}")
    return 0


def collide(args):
    """Any 4-gram shared across two different zones' fragments (plan 40 section 6 mandate 2)."""
    e = eras.get(args.era)
    rows = g.sql("SELECT zone_id, variant, angle, words FROM place_words "
                 f"WHERE era = {g.q(e['name'])} ORDER BY zone_id, variant")
    names = areas.load()
    grams = collections.defaultdict(set)
    for zid, variant, _angle, words in rows:
        w = re.findall(r"[a-z']+", words.lower())
        for i in range(len(w) - 3):
            grams[" ".join(w[i:i + 4])].add((int(zid), int(variant)))

    shared = {gram: where for gram, where in grams.items()
              if len({z for z, _ in where}) > 1}
    print(f"{len(rows)} fragments, {len(grams)} distinct 4-grams, "
          f"{len(shared)} shared across zones")
    for gram, where in sorted(shared.items(), key=lambda kv: -len(kv[1]))[:25]:
        places = ", ".join(f"{names.get(z, z)} v{v}" for z, v in sorted(where))
        print(f"  {len(where):2d}x  {gram!r}: {places}")

    if shared and args.prune:
        dropped = 0
        for gram, where in shared.items():
            for zid, variant in sorted(where)[1:]:       # keep the first, regenerate the rest
                g.sql(f"DELETE FROM place_words WHERE era = {g.q(e['name'])} "
                      f"AND zone_id = {zid} AND variant = {variant}", fetch=False)
                dropped += 1
        print(f"pruned {dropped} fragments; re-run `generate` to replace them")
    return 0


def report(args):
    e = eras.get(args.era)
    rows = g.sql("SELECT zone_id, COUNT(*) AS n, AVG(CHAR_LENGTH(words)) AS avg_len FROM place_words "
                 f"WHERE era = {g.q(e['name'])} GROUP BY zone_id ORDER BY zone_id")
    names = areas.load()
    usable = len(zone_rows(e, args.min_kinds))
    print(f"era {e['name']}: {len(rows)} of {usable} usable zones described")
    for zid, n, avg_len in rows:
        print(f"  {names.get(int(zid), zid):28s} {n} fragments, avg {float(avg_len):.0f} chars")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("sample", sample), ("generate", generate), ("collide", collide), ("report", report),
                     ("draft", draft), ("keep", keep)):
        p = sub.add_parser(name)
        p.set_defaults(fn=fn)
        p.add_argument("--era", required=True)
        p.add_argument("--zones", default="", help="comma-separated zone ids; default every usable zone")
        p.add_argument("--min-kinds", type=int, default=8,
                       help="a zone needs this many distinct creatures in the ledger to be worth describing")
        p.add_argument("--variants", type=int, default=len(ORDER), help=f"1..{len(ORDER)} angles per zone")
        p.add_argument("--limit", type=int, default=0)
        p.add_argument("--lanes", default="")
        p.add_argument("--slots", default="")
        p.add_argument("--no-judge", action="store_true")
        p.add_argument("--seed", type=int, default=None)
        p.add_argument("--per-lane", type=int, default=1, help="sample: zones per lane")
        p.add_argument("--rewrite", action="store_true", help="generate: replace fragments already held")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--prune", action="store_true", help="collide: delete the later copy of a shared phrase")
        p.add_argument("--only", default="", help="draft: just these zone:angle pairs, e.g. 1638:sight,1497:danger")
        p.add_argument("--save", default="", help="draft: write here; keep: store from here")
    args = ap.parse_args()
    if args.variants < 1 or args.variants > len(ORDER):
        sys.exit(f"--variants must be 1..{len(ORDER)}")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
