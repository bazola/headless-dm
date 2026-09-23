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

Why the seed is the ledger and not the world database: `creature.zoneId` is 0 for 144,979 of 150,172 spawns
and `areaId` for 146,271 -- both are optional caches, and the ids that are set belong to later expansions.
Resolving spawns to zones needs the core's map and DBC machinery, not SQL. The ledger already knows what this
realm has actually met, weighted by how often, which is the better question anyway (plan 46 section 2).
"""

import argparse
import collections
import os
import random
import re
import sys
import time

# realpath, not abspath: /opt/wow/lore is a symlink into the repo, and abspath does not follow one -- the
# same trap that took the lore gate down (plan 23 W2).
HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "regard"))

import areas                    # noqa: E402  the client's AreaTable.dbc, read off the box
import era as eras              # noqa: E402
import fleet                    # noqa: E402
import gen_backstories as g     # noqa: E402

SCHEMA = os.path.join(HERE, "place_tables.sql")

# Four angles, not four paraphrases (plan 40 section 6 mandate 1). A cached description is static text handed
# identically to every bot standing in the zone, which is the shape that made one register out of the cast.
ANGLES = {
    "sight": "what a traveller notices first here - the light, the ground, the sound, the smell of it",
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
MARKUP = re.compile(r'["“”*_#`\[\]]')

PROMPT = """You are a loremaster for {setting}
Everything you write is real life to the people in it. Never mention games, players, levels as numbers, specs, raids, loot, servers or anything outside the world.{limits}

The place: {zone}.
What travellers most often meet there: {creatures}.

Write ONE fragment about this place, from this angle: {angle}

Rules:
- At most 35 words, one or two sentences.
- A plain statement about the place, as someone who has been there would put it. No greeting, no dialogue, no quotation marks.
- NEVER advice, NEVER a warning, NEVER an instruction to anyone. Do not address a reader at all: no "you", no "your".
- No numbers, no lists, no markdown.
- Name the place itself or something that lives there, so it could not be mistaken for anywhere else.

Write only the fragment."""


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


def inhabitants(zone_id, limit=10):
    rows = g.sql("SELECT SUBSTRING_INDEX(SUBSTRING_INDEX(detail,'\"name\":\"',-1),'\"',1) AS who, "
                 "COUNT(*) AS n FROM ledger_event "
                 f"WHERE event_type = 'kill' AND zone_id = {int(zone_id)} "
                 f"GROUP BY who ORDER BY n DESC LIMIT {int(limit)}")
    return [r[0] for r in rows if r[0]]


# ---------------------------------------------------------------------------
# generation

def build(lane, e, zone, creatures, angle):
    prompt = PROMPT.format(setting=e["setting"], limits=e["limits"], zone=zone,
                           creatures=", ".join(creatures[:8]) or "little worth naming",
                           angle=ANGLES[angle])
    text = lane.chat([{"role": "user", "content": prompt}], 200, temperature=0.85)
    return clean(text)


def clean(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    text = text.strip().strip('"').strip()
    return " ".join(text.split())


def problems(text, e, zone, creatures, judge=None):
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
    if ADVICE.search(text):
        out.append("advice or instruction (plans/45)")
    m = e["meta"].search(text)
    if m:
        out.append(f"out of era: {m.group(0)!r}")
    low = text.lower()
    if zone.lower() not in low and not any(c.lower() in low for c in creatures):
        out.append("not grounded: names neither the place nor anything living in it")
    if not out and judge is not None:
        evidence = judge.check(eras.judge_prompt(e, text, known=creatures))
        if evidence:
            out.append(f"judge: {evidence}")
    return out


def one(lane, attempt, e, judge, zid, zone, creatures, angle, variant, store=True):
    """Generate, validate and (optionally) store a single fragment. Raises on failure so the pool retries."""
    text = build(lane, e, zone, creatures, angle)
    bad = problems(text, e, zone, creatures, judge)
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
        print(f"   met here: {', '.join(creatures[:6])}")
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
    for name, fn in (("sample", sample), ("generate", generate), ("collide", collide), ("report", report)):
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
    args = ap.parse_args()
    if args.variants < 1 or args.variants > len(ORDER):
        sys.exit(f"--variants must be 1..{len(ORDER)}")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
