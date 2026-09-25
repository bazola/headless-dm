#!/usr/bin/env python3
"""gen_quest_words.py - an errand said the way a person would say it (custom wow plans/50 section 6).

A quest-log title is a game artefact, like a level number: plan 32 invariant 1, "in-world words, never
figures". Five sites were handing titles straight to the model -- events.cpp:602/610, handler.cpp:1205/1288/
1293 -- and it did as it was told, so three bots announced "The Daughter Who Lived", "the Shadowy Figure
business" and "Worgen in the Woods" within seconds of real completions. Worse, the titles reached the
permanent memory store: 7.07% of 85,887 rows already carry one.

This renders each errand ONCE into something a person would actually say, into `quest_words`, and the module
reads that instead of the title. The same shape as `land_words` (plan 14 B3) and `place_words` (plan 46).

**NOTHING ABOUT AZEROTH IS WRITTEN INTO THIS REPOSITORY.** The quest list comes from this realm's own
`character_queststatus`, the text from the operator's own `acore_world`, and every phrase is stored in the
operator's own database. A realm nobody has played produces nothing, and two realms produce two different
sets.

    python3 gen_quest_words.py sample   --era classic [--quests 229,262]   # prints, stores nothing
    python3 gen_quest_words.py generate --era classic [--quests ...] [--rewrite] [--dry-run]
    python3 gen_quest_words.py collide  --era classic [--prune]            # cross-quest phrase collisions
    python3 gen_quest_words.py report   --era classic                      # coverage

Why only the quests this realm has touched: the world holds ~9,465 of them, but a played realm meets far
fewer (1,169 here), and the ones it has met are era-appropriate by construction -- the same reasoning that
made `gen_places.py` seed from the ledger rather than from the world database.
"""

import argparse
import collections
import os
import random
import re
import sys
import time

# realpath, not abspath: /opt/wow/lore is a symlink into the repo and abspath does not follow one -- the trap
# that took the lore gate down (plan 23 W2).
HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))          # services/, for common.site
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "regard"))   # areas: AreaTable.dbc off the box

import areas                    # noqa: E402  zone id -> name, read from the client, never committed
import era as eras              # noqa: E402
import fleet                    # noqa: E402
import gen_backstories as g     # noqa: E402
from common import site         # noqa: E402

SCHEMA = os.path.join(HERE, "quest_tables.sql")

# The world schema BY NAME, never hardcoded: gen_backstories learned this the hard way with `acore_auth`,
# which was invisible on every realm that kept the default four names and fatal on any realm that did not.
WORLD_DB = site.get("DB_WORLD", "acore_world")

# 3.3.5 quest text carries client control codes: $b/$B are line breaks, $N/$C/$R the reader's name, class and
# race, and $Gmale:female; picks by gender. Keep the first form of a gendered pair and drop the rest.
#
# MEASURED BUG, do not "simplify" this back into one pattern: a single greedy
# `\$[bBNncCrRgG](?:[^$]*\$[gG])?` let the optional tail run from a `$b` across real prose to a later `$G`,
# so "...$b$bMorgan may have $Gbeen:been; there" came out as "been:been; there" -- it deleted "Morgan may
# have". The objective text is what grounds the generated phrase, so eating it corrupts the prompt silently.
GENDERED = re.compile(r"\$[Gg]\s*([^:$;]*):[^;$]*;?")
CODE = re.compile(r"\$[A-Za-z]")

MARKUP = re.compile(r'["“”*_#`\[\]]')
SECOND_PERSON = re.compile(r"\b(you|your|yours|yourself)\b", re.I)
# plans/45: an errand phrased as an instruction installs the tic in the prompt itself, permanently.
ADVICE = re.compile(r"(?:^|[.!?]\s+)(keep|hold|watch|mind|guard|stay|beware|trust|avoid|remember|do not|don't)\b",
                    re.I)

# MEASURED in the first sample: half the output was the quest objective handed back as a command --
# "Kill eight kobold vermin and return to Marshal McBride", "Speak with Chef Grual...". That is the log in
# other words, which is the one thing this table exists to stop. A phrase must not open on a command verb.
IMPERATIVE = re.compile(r"^(kill|slay|speak|talk|bring|take|give|find|search|seek|return|report|deliver|"
                        r"collect|gather|retrieve|obtain|escort|defend|destroy|investigate|travel|go|meet|"
                        r"rescue|free|place|use|light|visit|locate|recover)\b", re.I)

# Also measured: "eight" walked past a `\d` check. Plan 32 invariant 1 is about figures, not digits, and a
# spelled-out count is a figure. "one" is deliberately absent -- "the one who lived" is good English.
NUMBER_WORD = re.compile(r"\b(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|"
                         r"thirty|forty|fifty|sixty|hundred|dozen|score)\b", re.I)


def spelled_count(text):
    """The number word only when it is a COUNT, never when it is part of a name.

    MEASURED: the bare regex threw away "speaking with Zarlman Two-Moons in Bloodhoof Village", because \\b
    matches at a hyphen. Azeroth is full of these -- Two-Moons, Thousand Needles, Thousand Blades. A count is
    lowercase and stands on its own; a name is capitalised or hyphenated. Match the shape you mean.
    """
    for m in NUMBER_WORD.finditer(text):
        word = m.group(0)
        if word[0].isupper():
            continue                                     # part of a name
        before = text[m.start() - 1] if m.start() else " "
        after = text[m.end()] if m.end() < len(text) else " "
        if before == "-" or after == "-":
            continue                                     # hyphenated name
        return word
    return None
STOP = set("the a an and or but of to in on at for with from is are was were be it its this that by "
            "into out up down over under near".split())

PROMPT = """You are a loremaster for {setting}
Everything you write is real life to the people in it. Never mention games, players, levels as numbers, specs, raids, loot, quest logs, servers or anything outside the world.{limits}

Someone in this world has taken on an errand. In their own records it is filed under the name "{title}", but nobody speaks like that. What they were actually asked to do: {objective}

Write how a person would refer to this errand in conversation - the thing itself, not its filed name.

It has to fit inside a sentence somebody says. Test it by reading these aloud with your phrase in place of the blank:
  "I am still at ____."      "____ is done."      "He asked me about ____."

Rules:
- At most 18 words. A NAMING PHRASE, never a sentence and never a command.
- Begin with a noun, with "the", or with an -ing word. NEVER begin with a command verb: not "Kill", not "Speak", not "Bring", not "Return".
- Do NOT hand the errand back as an order. "Kill eight kobold vermin and return to Marshal McBride" is WRONG - that is the log in other words. "the kobolds troubling Marshal McBride" is right.
- Good: "helping Farmer Saldean cull the harvest watchers" - "Willem's bounty on Padfoot" - "the book found at Sven's farm".
- NEVER reuse the filed name or any run of three words from it. If the filed name is "Wolves Across the Border", "the wolves crossing the border" is right and "Wolves Across the Border" is wrong.
- Name only what you were told above. Do not invent a place, a building or a title that was not given to you.
- No counts at all, in digits or in words: not "8", not "eight", not "a dozen".
- NEVER advice, NEVER a warning, NEVER an instruction. Do not address anyone: no "you", no "your".
- No quotation marks, no markdown.

Write only the phrase."""


# ---------------------------------------------------------------------------
# what this realm has actually taken on

def ensure_schema():
    # The whole file in one call, as gen_backstories.ensure_schema does it: splitting on ";" cuts a statement
    # in half when a COLUMN COMMENT contains one.
    with open(SCHEMA, encoding="utf-8") as fh:
        g.sql(fh.read(), fetch=False)


def clean_world_text(s):
    s = GENDERED.sub(r"\1", s or "")
    s = CODE.sub(" ", s)
    return " ".join(s.split())


CAPITALS = {1519, 1537, 1637, 1638, 1497, 1657}   # classic capitals: they collect a few later-age quests
                                                  # (Thunder Bluff and Darnassus both reach level 73), so a
                                                  # whole-zone verdict would condemn 28 real classic errands.


def out_of_era(e, sort_id, min_level, quest_level, zone_names, races=0, foreign_zones=frozenset()):
    """Why this errand belongs to a later age, or None. Both tests come from era.py's own facts.

    MEASURED, and it is why this exists: checking only the TITLE let 193 of 1,323 rendered errands through
    (14.6%) -- Netherstorm, Shattrath, Ulduar, Icecrown, Dragonblight. A quest with a bland name gives
    nothing away, and the era regex only caught it later, if the generated phrase happened to say a banned
    word; when it did not, a Burning Crusade errand was stored on a Classic realm.

      * LEVEL catches Outland and Northrend, since the era dict carries its own max_level.
      * ZONE NAME catches what level cannot: Eversong (4-9), Ghostlands (9-18) and Azuremyst (4-10) are
        starting zones of a later expansion sitting at Classic levels. The era's own `meta` regex already
        names them, so no hand-made id list is needed -- and `AllowableRaces` is NOT the test, because
        Eversong quests admit the classic Horde races too (690 & 255 = 178).
    """
    if min_level > e["max_level"] or quest_level > e["max_level"]:
        return f"level {min_level}-{quest_level}, past {e['name']}'s {e['max_level']}"
    zone = zone_names.get(sort_id, "") if sort_id > 0 else ""
    if zone and e["meta"].search(zone):
        return f"zone {zone!r}"
    # THIRD test, and it exists because the second one let Sunstrider Isle through. AreaTable names that
    # zone perfectly well -- the gap is that the era regex is a DENYLIST of places somebody remembered to
    # write down: it lists eversong, ghostlands, azuremyst and bloodmyst, but never "Sunstrider Isle" or
    # "Ammen Vale". `collide` found the survivors -- "sunspire on sunstrider isle" across five errands.
    #
    # Races settle exactly the cases the regex misses. A quest open only to races this age has never heard
    # of belongs to a later one: Sunstrider Isle is 512 (blood elf alone), the draenei vale 1024. It is not
    # a substitute for the zone test -- Eversong quests admit the classic Horde races too (690 & 255 = 178)
    # -- the two are complements.
    mask = 0
    for r in e["races"]:
        mask |= 1 << (r - 1)
    if races and not (races & mask):
        return f"open only to races outside {e['name']} (AllowableRaces {races})"
    # FOURTH, and it closes the last hole the other three leave. Ammen Vale kept two errands that pass all
    # of the above: "Urgent Delivery!" is open to every race (AllowableRaces 0) and "Volatile Mutations"
    # admits the classic ones -- yet both happen in a zone that does not exist in this age. So a zone judged
    # foreign on its OWN evidence condemns the quests inside it. Capitals are held out by the set above.
    if sort_id in foreign_zones:
        return f"in a later age's land: {zone_names.get(sort_id, sort_id)!r}"
    return None


def quest_rows(e, args):
    """(quest_id, title, objective) for every quest anyone on this realm has taken on.

    Both status tables, because a quest that is finished and rewarded is exactly the one a bot is most likely
    to mention. Titles out of era are skipped the way gen_places skips a later age's zones.
    """
    ids = {int(r[0]) for r in g.sql(
        "SELECT DISTINCT quest FROM character_queststatus WHERE quest > 0 "
        "UNION SELECT DISTINCT quest FROM character_queststatus_rewarded WHERE quest > 0")}
    if args.quests:
        want = {int(x) for x in args.quests.split(",") if x.strip()}
        missing = want - ids
        if missing:
            g.log(f"not taken on by anyone on this realm: {sorted(missing)}")
        ids &= want
    if not ids:
        return []

    out = []
    zone_names = areas.load()
    # One query, not one per quest. The id list is this realm's own, so it is bounded by what has been played.
    rows = g.sql(f"SELECT ID, LogTitle, LogDescription, QuestDescription, QuestSortID, MinLevel, QuestLevel, "
                 f"AllowableRaces FROM {WORLD_DB}.quest_template "
                 f"WHERE ID IN ({','.join(str(i) for i in sorted(ids))})")

    # Which LANDS belong to a later age, judged by their own quests rather than by a list I typed. A zone is
    # condemned if its name trips the era regex, if anything in it runs past the era's ceiling, or if it
    # holds a quest open only to races this age has never heard of.
    mask = 0
    for r in e["races"]:
        mask |= 1 << (r - 1)
    evidence = {}
    for _qid, _t, _ld, _qd, sort_id, min_level, quest_level, races in rows:
        sort_id = int(sort_id)
        if sort_id <= 0:
            continue
        z = evidence.setdefault(sort_id, dict(maxlvl=0, regex=False, race_foreign=0))
        z["maxlvl"] = max(z["maxlvl"], int(min_level), int(quest_level))
        nm = zone_names.get(sort_id, "")
        if nm and e["meta"].search(nm):
            z["regex"] = True
        if int(races) and not (int(races) & mask):
            z["race_foreign"] += 1
    foreign_zones = frozenset(
        z for z, d in evidence.items()
        if z not in CAPITALS and (d["regex"] or d["maxlvl"] > e["max_level"] or d["race_foreign"]))

    for qid, title, logdesc, questdesc, sort_id, min_level, quest_level, races in rows:
        title = clean_world_text(title)
        if len(title) < 3:
            continue
        if e["meta"].search(title):
            continue                      # a later age's errand, by its name
        later = out_of_era(e, int(sort_id), int(min_level), int(quest_level), zone_names,
                           int(races), foreign_zones)
        if later:
            continue                      # a later age's errand, by where, when, or who may take it
        objective = clean_world_text(logdesc) or clean_world_text(questdesc)[:400]
        if not objective:
            continue                      # nothing to render it from
        out.append((int(qid), title, objective))
    out.sort()
    if args.limit:
        out = out[:args.limit]
    return out


# ---------------------------------------------------------------------------
# generation

def build(lane, e, title, objective):
    prompt = PROMPT.format(setting=e["setting"], limits=e["limits"], title=title, objective=objective)
    text = lane.chat([{"role": "user", "content": prompt}], 120, temperature=0.85)
    return clean(text)


def clean(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    text = text.strip().strip('"').strip().rstrip(".")
    return " ".join(text.split())


def title_runs(title, n=3):
    """Every run of n words from the filed name, lowercased. Reusing one is the failure this table exists to
    prevent, so it is checked mechanically rather than trusted to the prompt."""
    w = [x for x in re.findall(r"[a-z']+", title.lower())]
    return {" ".join(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}


def known_names(*texts):
    """Proper nouns out of the realm's OWN quest text, handed to the judge as `known`.

    Without them the small judge flags a real NPC or village as an invented company (era.judge_prompt's own
    docstring says so), which would reject perfectly good phrases naming Watcher Sarah Ladimore or Darkshire.
    A capitalised word that opens a sentence is skipped -- "Speak with ..." is not a person.
    """
    found = set()
    for t in texts:
        for m in re.finditer(r"\b[A-Z][a-z']+(?:\s+(?:of|the)?\s*[A-Z][a-z']+)*", t or ""):
            s = " ".join(m.group(0).split())
            multiword = " " in s
            at_start = m.start() == 0 or t[max(0, m.start() - 2):m.start()].strip() in (".", "!", "?", "")
            if len(s) > 3 and (multiword or not at_start):
                found.add(s)
    return found


def problems(text, e, title, objective, judge=None):
    out = []
    if not text:
        return ["empty"]
    words = re.findall(r"[A-Za-z0-9']+", text)
    low = text.lower()
    if len(text) > 255:
        out.append(f"{len(text)} chars over 255")
    if len(words) > 22:
        out.append(f"{len(words)} words over 22")
    if re.search(r"\d", text):
        out.append("contains a number")
    counted = spelled_count(text)
    if counted:
        out.append(f"spelled-out count: {counted!r}")
    if IMPERATIVE.match(text):
        out.append(f"the log restated as a command: opens on {IMPERATIVE.match(text).group(0)!r}")
    if MARKUP.search(text):
        out.append("quotes or markdown")
    if SECOND_PERSON.search(text):
        out.append("addresses the reader")
    if ADVICE.search(text):
        out.append("advice or instruction (plans/45)")
    # Parroting the log is the defect; NAMING the thing the errand is about is not. A great many titles are
    # simply a person, a place or the object itself -- "Raven Hill", "Southshore", "Zalazane", "Raptor Horns"
    # -- and you cannot refer to that errand without saying it.
    #
    # MEASURED, and it cost 8 of the first 17 failures: the blunt rule threw away "the spirits haunting Raven
    # Hill", "speaking with Dibbs in Southshore", "searching for the Greenwarden" and "the battleboars of
    # Camp Narache", every one of them right. So a run from the filed name only counts as parroting when the
    # OBJECTIVE does not contain it too: what the errand is really about is always sayable, and only the
    # log's own turn of phrase is barred. "Wolves Across the Border" is still refused; "the wolves crossing
    # the border" was always fine and now passes for the right reason.
    # Articles carry no naming content, so they must not take part in this comparison. MEASURED: without
    # this, "the battleboars of Camp Narache" was refused against the objective "Kill 6 Battleboars near
    # Camp Narache" -- because the objective happens not to contain the word "the". A correct phrase died on
    # an article.
    obj_words = set(re.findall(r"[a-z']+", objective.lower())) - STOP
    title_words = set(re.findall(r"[a-z']+", title.lower())) - STOP
    if title.lower() in low and not title_words <= obj_words:
        out.append("repeats the filed name")
    shared = {r for r in (title_runs(title) & title_runs(text))
              if not (set(r.split()) - STOP) <= obj_words}
    if shared:
        out.append(f"reuses the filed name's wording: {sorted(shared)[0]!r}")
    m = e["meta"].search(text)
    if m:
        out.append(f"out of era: {m.group(0)!r}")
    # Grounded: it must carry at least one substantial word from what was actually asked, or it could be any
    # errand at all -- the failure mode this whole table exists to avoid.
    asked = {w for w in re.findall(r"[a-z']{4,}", objective.lower()) if w not in STOP}
    if asked and not (asked & {w for w in re.findall(r"[a-z']{4,}", low)}):
        out.append("not grounded: names nothing from what was actually asked")
    if not out and judge is not None:
        # The objective only, never the title: handing the judge the filed name would tell it that the exact
        # wording this table exists to avoid is acceptable.
        evidence = judge.check(eras.judge_prompt(e, text, known=known_names(objective)))
        if evidence:
            out.append(f"judge: {evidence}")
    return out


def one(lane, attempt, e, judge, qid, title, objective, variant, store=True):
    """Generate, validate and (optionally) store one phrase. Raises on failure so the pool retries."""
    text = build(lane, e, title, objective)
    bad = problems(text, e, title, objective, judge)
    if bad:
        raise ValueError("; ".join(bad) + f" :: {text[:90]}")
    if store:
        g.sql("INSERT INTO quest_words (quest_id, era, variant, words) VALUES "
              f"({int(qid)}, {g.q(e['name'])}, {int(variant)}, {g.q(text)}) "
              "ON DUPLICATE KEY UPDATE words = VALUES(words), updated_at = NOW()",
              fetch=False)
    return text


def lanes_for(args):
    chosen = [l for l in fleet.pick_lanes(args.lanes, args.slots) if "character" in l.kinds]
    if not chosen:
        sys.exit("no batch lane carries the 'character' kind; check site/fleet.toml")
    return chosen


# ---------------------------------------------------------------------------
# commands

def generate(args):
    e = eras.get(args.era)
    ensure_schema()
    lanes = lanes_for(args)
    judge = None if args.no_judge else fleet.Judge()
    rows = quest_rows(e, args)
    if not rows:
        sys.exit("no errands to render: has this realm been played?")

    have = {(int(q), int(v)) for q, v in
            g.sql(f"SELECT quest_id, variant FROM quest_words WHERE era = {g.q(e['name'])}")}
    jobs = []
    for qid, title, objective in rows:
        for variant in range(args.variants):
            if not args.rewrite and (qid, variant) in have:
                continue
            jobs.append((qid, title, objective, variant))

    g.log(f"era {e['name']}; {len(rows)} errands this realm has taken on, {len(jobs)} phrases to write "
          f"({len(have)} already held); lanes: " + ", ".join(f"{l.name}x{l.slots}" for l in lanes))
    if args.dry_run:
        for qid, title, objective, variant in jobs[:40]:
            g.log(f"  would write {qid} v{variant}: {title!r} <- {objective[:70]!r}")
        if len(jobs) > 40:
            g.log(f"  ... and {len(jobs) - 40} more")
        return 0

    pool = fleet.Pool(lanes, log=g.log)
    for qid, title, objective, variant in jobs:
        pool.submit("character", 1,
                    lambda lane, attempt, a=(qid, title, objective, variant):
                        one(lane, attempt, e, judge, *a),
                    f"quest {qid} [{title[:34]}]")
    failures = pool.run()
    for j in failures:
        g.log(f"FAILED {j['label']}: {' | '.join(j['errors'])}")
    held = g.sql(f"SELECT COUNT(*) FROM quest_words WHERE era = {g.q(e['name'])}")[0][0]
    g.log(f"done: {held} phrases held for era {e['name']}; {len(failures)} failed")
    g.log("run `collide` before the module is pointed at this table (plan 50 section 6)")
    return 1 if failures else 0


def sample(args):
    """Print phrases for a few errands on each lane; stores nothing."""
    e = eras.get(args.era)
    lanes = lanes_for(args)
    judge = None if args.no_judge else fleet.Judge()
    rows = quest_rows(e, args) or []
    if not rows:
        sys.exit("no errands to render: has this realm been played?")
    rng = random.Random(args.seed)
    if not args.quests:
        rng.shuffle(rows)
        rows = rows[:args.per_lane * len(lanes)]

    for i, (qid, title, objective) in enumerate(rows):
        lane = lanes[i % len(lanes)]
        print(f"\n== {qid}  lane {lane.name}")
        print(f"   filed as: {title}")
        print(f"   asked for: {objective[:110]}")
        for variant in range(args.variants):
            t0 = time.time()
            try:
                text = one(lane, 0, e, judge, qid, title, objective, variant, store=False)
                print(f"   [v{variant}] {text}   ({time.time() - t0:.0f}s)")
            except Exception as ex:
                print(f"   [v{variant}] REJECTED {ex}")
    return 0


def collide(args):
    """Any 4-gram shared across two different errands' phrases. A phrase that fits every quest names none."""
    e = eras.get(args.era)
    rows = g.sql("SELECT quest_id, variant, words FROM quest_words "
                 f"WHERE era = {g.q(e['name'])} ORDER BY quest_id, variant")
    grams = collections.defaultdict(set)
    for qid, variant, words in rows:
        w = re.findall(r"[a-z']+", words.lower())
        for i in range(len(w) - 3):
            grams[" ".join(w[i:i + 4])].add((int(qid), int(variant)))
    shared = {gram: where for gram, where in grams.items() if len({q for q, _ in where}) > 1}
    print(f"{len(rows)} phrases, {len(grams)} distinct 4-grams, {len(shared)} shared across errands")
    for gram, where in sorted(shared.items(), key=lambda kv: -len(kv[1]))[:25]:
        print(f"  {len(where):2d}x  {gram!r}: " + ", ".join(f"{q} v{v}" for q, v in sorted(where)))
    if shared and args.prune:
        dropped = 0
        for gram, where in shared.items():
            for qid, variant in sorted(where)[1:]:       # keep the first, regenerate the rest
                g.sql(f"DELETE FROM quest_words WHERE era = {g.q(e['name'])} "
                      f"AND quest_id = {qid} AND variant = {variant}", fetch=False)
                dropped += 1
        print(f"pruned {dropped} phrases; re-run `generate` to replace them")
    return 0


def report(args):
    e = eras.get(args.era)
    held = g.sql("SELECT COUNT(*), AVG(CHAR_LENGTH(words)) FROM quest_words "
                 f"WHERE era = {g.q(e['name'])}")[0]
    usable = len(quest_rows(e, args))
    n = int(held[0] or 0)
    print(f"era {e['name']}: {n} of {usable} errands rendered"
          + (f", avg {float(held[1]):.0f} chars" if n else ""))
    if n:
        for qid, words in g.sql("SELECT quest_id, words FROM quest_words "
                                f"WHERE era = {g.q(e['name'])} ORDER BY RAND() LIMIT 8"):
            print(f"  {qid:6s} {words}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("sample", sample), ("generate", generate), ("collide", collide), ("report", report)):
        p = sub.add_parser(name)
        p.set_defaults(fn=fn)
        p.add_argument("--era", required=True)
        p.add_argument("--quests", default="", help="comma-separated quest ids; default every one played here")
        p.add_argument("--variants", type=int, default=1, help="phrasings per errand")
        p.add_argument("--limit", type=int, default=0)
        p.add_argument("--lanes", default="")
        p.add_argument("--slots", default="")
        p.add_argument("--no-judge", action="store_true")
        p.add_argument("--seed", type=int, default=None)
        p.add_argument("--per-lane", type=int, default=2, help="sample: errands per lane")
        p.add_argument("--rewrite", action="store_true", help="generate: replace phrases already held")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--prune", action="store_true", help="collide: delete the later copy of a shared phrase")
    args = ap.parse_args()
    if args.variants < 1:
        sys.exit("--variants must be at least 1")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
