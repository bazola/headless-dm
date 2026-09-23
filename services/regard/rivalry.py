#!/usr/bin/env python3
"""Guild seats and relations: companies as small fiefdoms.

custom wow plans/14, step B1. Seeded once from the guild histories (lore_guild) on the fleet's guild
lanes, then kept in acore_characters.guild_seat and guild_relation. Incidents (step B2) move stances
later; prompts (B3) and the dashboard map (B4) read these tables.

  seats      every company keeps a seat (a hall, camp or claim) in one land for each kind of country
             its people travel: recruits (levels 1-20), blooded (20-40), seasoned (40-60). Lands are the
             company's own faction's or contested ones. The model ranks three lands per band from the
             company's history; seats are then handed out best choice first with a cap per land, so the
             nine Horde companies don't all sit in the Barrens but some ground is shared. A company whose
             choices were all taken gets another call listing only the lands still open.
  relations  every two companies with a seat in the same land get a relation; a company left with none
             is paired with one of its own faction. The disposition (enemies, rivals, wary, respect,
             allies) is drawn by weight; the model writes how it came to be from both histories. Every
             company ends with at least one rival or enemy.
  regard     stance moves the baseline between members of the two companies (regard.STANCE_SHARE), so
             a feud shows up in personal feeling.

Standing rule: bots are people living in Azeroth. Everything written is in-world.

Usage:
  python3 rivalry.py sample --era classic --guilds 6,9    seat choices for those companies and one relation
                                                          between the first two; stores nothing
  python3 rivalry.py seed --era classic [--reseed] [--seed N]
  python3 rivalry.py show [NAME]
"""
import argparse
import json
import os
import random
import re
import sys

import regard as rg  # also puts /opt/wow/lore on the path
import era as eras  # noqa: E402
import fleet  # noqa: E402
from gen_backstories import OVERUSED_NAMES, OVERUSED_RE  # noqa: E402
from lands import LAND_NOTES, ZONES

HERE = os.path.dirname(os.path.abspath(__file__))
log = rg.log

# (key, lowest level, highest level, how the prompt names that country)
BANDS = [
    ("recruits", 1, 20, "the gentler lands where its newest recruits learn the road"),
    ("blooded", 20, 40, "the frontier where its blooded members do their fighting"),
    ("seasoned", 40, 60, "the dangerous country where its most seasoned members serve"),
]
BAND_OVERLAP = 5            # a land belongs to a band when their level ranges share this many levels
SEATS_PER_LAND = 3          # companies seated in one land, all bands together
SEATS_PER_LAND_FACTION = 2  # ... of which from one faction; the last round allows one more of each
SEAT_ROUNDS = 3
CHOICES = 3

# key: (stance, what lies between the two companies)
DISPOSITIONS = {
    "enemies": (-70, "open hatred: blood has been spilled between them and neither side will forgive it"),
    "rivals": (-45, "a bitter rivalry: they compete for the same ground, the same work and the same renown"),
    "wary": (-20, "wary distrust: old slights and suspicion, though no open quarrel"),
    "respect": (15, "grudging respect: little love between them, but each has seen the other's worth"),
    "allies": (45, "friendship: they have fought side by side and owe each other debts"),
}
STANCE_JITTER = 7
# (same faction, share a seat) -> weights
DRAW_WEIGHTS = {
    (True, True): {"enemies": 3, "rivals": 35, "wary": 27, "respect": 15, "allies": 20},
    (False, True): {"enemies": 25, "rivals": 40, "wary": 25, "respect": 10},
    (True, False): {"rivals": 25, "wary": 20, "respect": 20, "allies": 35},
}
HOSTILE = ("enemies", "rivals")

PREAMBLE = ("You are a loremaster for {setting} Everything you write is real history to the people in it. "
            "Never mention games, players, levels as numbers, zones, servers or anything outside the world.")

SEAT_PROMPT = PREAMBLE + """

The {faction} company "{name}".
Its charter: {charter}
Its history: {history}

A company with members of every seasoning keeps a seat (a hall, a camp, a watchtower, a claim) in each kind of country its people travel. Choose where this company keeps its seats, true to its history and to where the {faction} can stand. Each land lists what stands there: keep every place in its own land and never put a {faction} company inside the other faction's town. Small places of your own invention are fine.

{asks}

Reply with ONLY a JSON object, no markdown fences:
{{{shape}}}"""

RELATION_PROMPT = PREAMBLE + """

Two companies:
"{a}", of the {fa}. Its charter: {ca}
Its history: {ha}

"{b}", of the {fb}. Its charter: {cb}
Its history: {hb}

{ground}
Between them there is {disposition}.

Write how that came to be. Build on both histories and never contradict them; invent the moments between the companies freely. Anyone named in the histories is still alive: they may be wounded or shamed, never killed. Don't use the names {overused}.
Reply with ONLY a JSON object, no markdown fences:
{{
  "origin": "40 to 80 words: how it began and how things stand between the two companies now",
  "moments": ["two or three moments between the companies, each under 20 words"],
  "a_says": "what members of {a} say of {b}, in their own voice, under 20 words",
  "b_says": "what members of {b} say of {a}, in their own voice, under 20 words"
}}"""


def extract_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON object in reply: {text[:120]!r}")
    return json.loads(m.group(0))


def clean(text):
    return re.sub(r"\s+", " ", str(text or "").translate(rg.ASCII_PUNCT)).strip().strip('"')


def norm(name):
    return re.sub(r"^the\s+", "", clean(name).lower())


def flat(col):
    return f"REPLACE(REPLACE(COALESCE({col}, ''), CHAR(10), ' '), CHAR(9), ' ')"


def model_name(lane):
    return (lane.name + ":" + lane.model)[:64]


def ensure_schema():
    rg.sql(open(os.path.join(HERE, "rivalry_tables.sql")).read(), fetch=False)


def guild_lanes():
    return [lane for lane in fleet.pick_lanes() if "guild" in lane.kinds]


def companies(e):
    races, classes = ",".join(map(str, sorted(e["races"]))), ",".join(map(str, sorted(e["classes"])))
    horde = ",".join(map(str, sorted(rg.HORDE)))
    rows = rg.sql(f"SELECT g.guildid, g.name, {flat('lg.charter')}, {flat('lg.history')}, "
                  f"SUM(c.race IN ({horde})), COUNT(*) FROM guild g "
                  "JOIN lore_guild lg ON lg.guildid = g.guildid JOIN guild_member m ON m.guildid = g.guildid "
                  "JOIN characters c ON c.guid = m.guid "
                  f"WHERE lg.era = {rg.q(e['name'])} AND c.race IN ({races}) AND c.class IN ({classes}) "
                  "GROUP BY g.guildid ORDER BY g.guildid")
    return [dict(guildid=int(r[0]), name=r[1], charter=r[2], history=r[3],
                 faction="Horde" if 2 * int(r[4]) > int(r[5]) else "Alliance") for r in rows]


def vet(text, e, judge, attempt, label):
    """Raise to retry; on the last attempt a judge flag is recorded for review instead."""
    bad = e["meta"].search(text)
    if bad:
        raise ValueError(f"out-of-world term: {bad.group(0)!r}")
    name = OVERUSED_RE.search(text)
    if name:
        raise ValueError(f"overused name {name.group(0)!r}")
    evidence = judge.check(eras.judge_prompt(e, text)) if judge else None
    if not evidence:
        return
    if attempt < fleet.MAX_ATTEMPTS:
        raise ValueError(f"judge: {evidence}")
    with open(os.path.join(HERE, f"review-rivalry-{e['name']}.jsonl"), "a") as f:
        f.write(json.dumps({"label": label, "evidence": evidence, "text": text}) + "\n")


# ---------------------------------------------------------------------------
# seats

def band_lands(zones, band, faction):
    own = "H" if faction == "Horde" else "A"
    _, low, high, _ = band
    return [z for z in zones if z[4] in (own, "C") and min(high, z[3]) - max(low, z[2]) >= BAND_OVERLAP]


def open_lands(zones, placed, guild_by, g, band, relax):
    """Lands this company can still take for the band, given the seats already handed out."""
    held, total, same = set(), {}, {}
    for (gid, _), seat in placed.items():
        zid = seat[0][0]
        if gid == g["guildid"]:
            held.add(zid)
        total[zid] = total.get(zid, 0) + 1
        if guild_by[gid]["faction"] == g["faction"]:
            same[zid] = same.get(zid, 0) + 1
    extra = 1 if relax else 0
    return [z for z in band_lands(zones, band, g["faction"]) if z[0] not in held
            and total.get(z[0], 0) < SEATS_PER_LAND + extra and same.get(z[0], 0) < SEATS_PER_LAND_FACTION + extra]


def resolve_land(answer, lands):
    """The land a model's answer names: the land itself, a phrase containing it, or a place from its notes
    (a sample answered "Razor Hill" for Durotar)."""
    answer = norm(answer)
    for z in lands:
        if norm(z[1]) == answer:
            return z
    for z in lands:
        if norm(z[1]) in answer:
            return z
    for z in lands:
        places = [norm(re.sub(r"\(.*?\)", "", p)) for p in LAND_NOTES[z[0]].split(",")]
        if any(p and (p == answer or re.sub(r"^the\s+", "", p) == answer) for p in places):
            return z
    return None


def propose(lane, attempt, e, judge, g, asks):
    """asks: [(band, lands, n)]. Returns {band key: [(zone, hold), ...]} best first."""
    lines, shape = [], []
    for band, lands, n in asks:
        lines.append(f"For {band[3]}, choose {n} of these lands:\n"
                     + "\n".join(f"- {z[1]}: {LAND_NOTES[z[0]]}" for z in lands))
        item = '{"land": "a name exactly as listed", "hold": "one sentence of 12 to 30 words: what the company keeps there and why"}'
        shape.append(f'"{band[0]}": [' + ", ".join([item] * n) + "]")
    prompt = SEAT_PROMPT.format(setting=e["setting"], faction=g["faction"], name=g["name"], charter=g["charter"],
                                history=g["history"], asks="\n".join(lines), shape=", ".join(shape))
    raw = extract_json(lane.chat([{"role": "user", "content": prompt}], 250 + 150 * sum(n for *_, n in asks),
                                 temperature=0.8))
    picks, texts = {}, []
    for band, lands, n in asks:
        chosen = []
        for item in raw.get(band[0]) or []:
            if not isinstance(item, dict):
                continue
            zone, hold = resolve_land(item.get("land"), lands), clean(item.get("hold"))
            if zone and zone not in [c[0] for c in chosen] and 8 <= len(hold.split()) <= 40:
                chosen.append((zone, hold))
        if not chosen:
            raise ValueError(f"no usable land for {band[0]}: {str(raw.get(band[0]))[:100]}")
        picks[band[0]] = chosen[:n]
        texts += [hold for _, hold in chosen[:n]]
    vet(" ".join(texts), e, judge, attempt, f"seats {g['guildid']} {g['name']}")
    return picks


def seat_round(lanes, e, judge, jobs):
    results = {}
    pool = fleet.Pool(lanes, log=log)
    for g, asks in jobs:
        def run(lane, attempt, g=g, asks=asks):
            picks = propose(lane, attempt, e, judge, g, asks)
            results[g["guildid"]] = (picks, model_name(lane))
            return "; ".join(f"{b}: {', '.join(z[1] for z, _ in p)}" for b, p in picks.items())
        pool.submit("guild", 0, run, f"seats {g['guildid']} {g['name']}")
    pool.run()
    return results


def seed_seats(e, lanes, judge, guilds, rng):
    zones = {z[0]: z for z in ZONES[e["name"]]}
    guild_by = {g["guildid"]: g for g in guilds}
    placed = {(int(r[0]), r[1]): (zones[int(r[2])], r[3])
              for r in rg.sql(f"SELECT guildid, band, zone_id, {flat('hold')} FROM guild_seat WHERE era = {rg.q(e['name'])}")
              if int(r[0]) in guild_by and int(r[2]) in zones}
    for round_no in range(1, SEAT_ROUNDS + 1):
        relax = round_no == SEAT_ROUNDS
        jobs = {}
        for g in guilds:
            for band in BANDS:
                if (g["guildid"], band[0]) in placed:
                    continue
                lands = open_lands(zones.values(), placed, guild_by, g, band, relax)
                if lands:
                    n = min(CHOICES if round_no == 1 else 1, len(lands))
                    jobs.setdefault(g["guildid"], (g, []))[1].append((band, lands, n))
        if not jobs:
            break
        log(f"seat round {round_no}: {sum(len(a) for _, a in jobs.values())} seats to choose for {len(jobs)} companies")
        results = seat_round(lanes, e, judge, list(jobs.values()))
        for band in BANDS:
            order = sorted(results)
            rng.shuffle(order)
            for gid in order:
                g, (picks, model) = guild_by[gid], results[gid]
                if (gid, band[0]) in placed:
                    continue
                free = {z[0] for z in open_lands(zones.values(), placed, guild_by, g, band, relax)}
                for rank, (zone, hold) in enumerate(picks.get(band[0], []), 1):
                    if zone[0] in free:
                        placed[(gid, band[0])] = (zone, hold)
                        rg.sql("INSERT INTO guild_seat (guildid, band, zone_id, zone_name, hold, model, era) VALUES "
                               f"({gid}, {rg.q(band[0])}, {zone[0]}, {rg.q(zone[1])}, {rg.q(hold[:255])}, "
                               f"{rg.q(model)}, {rg.q(e['name'])})", fetch=False)
                        if round_no == 1 and rank > 1:
                            log(f"  {g['name']} {band[0]}: choice {rank}, {zone[1]}")
                        break
    missing = [(guild_by[gid]["name"], b[0]) for gid in guild_by for b in BANDS if (gid, b[0]) not in placed]
    for name, band in missing:
        log(f"no seat for {name} ({band})")
    return placed


# ---------------------------------------------------------------------------
# relations

def draw_relations(guilds, placed, rng):
    """Returns {(a, b): (disposition, [shared zones])} with a < b."""
    faction = {g["guildid"]: g["faction"] for g in guilds}
    by_zone = {}
    for (gid, _), (zone, _) in placed.items():
        by_zone.setdefault(zone, set()).add(gid)
    shared = {}
    for zone, gids in sorted(by_zone.items()):
        for a in gids:
            for b in gids:
                if a < b:
                    shared.setdefault((a, b), []).append(zone)
    pairs = {}
    for (a, b), zones in sorted(shared.items()):
        pairs[(a, b)] = [None, zones]
    for g in guilds:  # a company that shares no land gets one relation within its own faction
        if any(g["guildid"] in k for k in pairs):
            continue
        mates = [o for o in guilds if o["guildid"] != g["guildid"] and o["faction"] == g["faction"]]
        if mates:
            o = min(mates, key=lambda m: (sum(m["guildid"] in k for k in pairs), rng.random()))
            pairs[tuple(sorted((g["guildid"], o["guildid"])))] = [None, []]
    for (a, b), item in pairs.items():
        weights = DRAW_WEIGHTS[(faction[a] == faction[b], bool(item[1]))]
        item[0] = rng.choices(list(weights), weights=list(weights.values()))[0]
    for g in guilds:  # every company has someone to resent
        mine = [(k, v) for k, v in pairs.items() if g["guildid"] in k]
        if mine and not any(v[0] in HOSTILE for _, v in mine):
            k, v = max(mine, key=lambda kv: (kv[1][0] != "allies", len(kv[1][1]), rng.random()))
            v[0] = "rivals"
    return {k: tuple(v) for k, v in pairs.items()}


def relation_prompt(e, ga, gb, disposition, zones):
    if zones:
        ground = f"Both keep seats in {' and '.join(z[1] for z in zones)}, so their people cross paths there."
    else:
        ground = f"Both serve the {ga['faction']}, and their people cross paths on the road."
    return RELATION_PROMPT.format(setting=e["setting"], a=ga["name"], fa=ga["faction"], ca=ga["charter"],
                                  ha=ga["history"], b=gb["name"], fb=gb["faction"], cb=gb["charter"],
                                  hb=gb["history"], ground=ground, disposition=DISPOSITIONS[disposition][1],
                                  overused=", ".join(OVERUSED_NAMES))


def build_relation(lane, attempt, e, judge, ga, gb, disposition, zones):
    raw = extract_json(lane.chat([{"role": "user", "content": relation_prompt(e, ga, gb, disposition, zones)}],
                                 700, temperature=0.85))
    origin, a_says, b_says = clean(raw.get("origin")), clean(raw.get("a_says")), clean(raw.get("b_says"))
    moments = [clean(m) for m in raw.get("moments") or [] if isinstance(m, str) and clean(m)]
    if not 30 <= len(origin.split()) <= 110:
        raise ValueError(f"origin has {len(origin.split())} words")
    if not 1 <= len(moments) <= 4 or any(not 3 <= len(m.split()) <= 30 for m in moments):
        raise ValueError(f"moments unusable: {moments}")
    for key, said in (("a_says", a_says), ("b_says", b_says)):
        if not 3 <= len(said.split()) <= 30:
            raise ValueError(f"{key} has {len(said.split())} words")
    vet(" ".join([origin, *moments, a_says, b_says]), e, judge, attempt, f"relation {ga['name']} / {gb['name']}")
    return dict(origin=origin, moments=moments[:3], a_says=a_says, b_says=b_says)


def seed_relations(e, lanes, judge, guilds, placed, rng):
    guild_by = {g["guildid"]: g for g in guilds}
    pairs = draw_relations(guilds, placed, rng)
    counts = {}
    for disposition, _ in pairs.values():
        counts[disposition] = counts.get(disposition, 0) + 1
    log(f"{len(pairs)} relations drawn: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    pool = fleet.Pool(lanes, log=log)
    for (a, b), (disposition, zones) in sorted(pairs.items()):
        stance = round(DISPOSITIONS[disposition][0] + rng.uniform(-STANCE_JITTER, STANCE_JITTER), 1)

        def run(lane, attempt, a=a, b=b, disposition=disposition, zones=zones, stance=stance):
            r = build_relation(lane, attempt, e, judge, guild_by[a], guild_by[b], disposition, zones)
            rg.sql("INSERT INTO guild_relation (guild_a, guild_b, stance, disposition, shared_zones, origin, moments, "
                   "a_says, b_says, model, era) VALUES "
                   f"({a}, {b}, {stance}, {rg.q(disposition)}, {rg.q(json.dumps([z[0] for z in zones]))}, "
                   f"{rg.q(r['origin'])}, {rg.q(json.dumps(r['moments']))}, {rg.q(r['a_says'][:160])}, "
                   f"{rg.q(r['b_says'][:160])}, {rg.q(model_name(lane))}, {rg.q(e['name'])})", fetch=False)
            return disposition
        pool.submit("guild", 0, run, f"relation {guild_by[a]['name']} / {guild_by[b]['name']}")
    return pool.run()


# ---------------------------------------------------------------------------
# commands

def seed(args):
    e = eras.get(args.era)
    if e["name"] not in ZONES:
        sys.exit(f"no lands defined for era {e['name']}")
    ensure_schema()
    if args.reseed:
        touched = int(rg.sql("SELECT COUNT(*) FROM guild_relation WHERE last_incident_at IS NOT NULL")[0][0])
        if touched and not args.force:
            sys.exit(f"{touched} relations have incidents since seeding; --force to discard them")
        rg.sql(f"DELETE FROM guild_relation WHERE era = {rg.q(e['name'])}; "
               f"DELETE FROM guild_seat WHERE era = {rg.q(e['name'])};", fetch=False)
        log("cleared seats and relations for the era")
    rng = random.Random(args.seed)
    guilds = companies(e)
    lanes = guild_lanes()
    judge = fleet.Judge()
    log(f"era {e['name']}; {len(guilds)} companies; lanes: " + ", ".join(f"{l.name}x{l.slots}" for l in lanes))

    placed = seed_seats(e, lanes, judge, guilds, rng)
    if len(placed) < len(guilds) * len(BANDS):
        log("seats incomplete: run seed again before relations are drawn")
        return 1
    if int(rg.sql(f"SELECT COUNT(*) FROM guild_relation WHERE era = {rg.q(e['name'])}")[0][0]):
        log("relations already seeded (--reseed to start over)")
    else:
        failures = seed_relations(e, lanes, judge, guilds, placed, rng)
        for j in failures:
            log(f"FAILED {j['label']}: {' | '.join(j['errors'])}")
    changed = rg.refresh_guild_baselines()
    log(f"regard baselines refreshed for {changed} feelings between members of different companies")
    show(argparse.Namespace(name=None))
    return 0


def sample(args):
    e = eras.get(args.era)
    zones = ZONES[e["name"]]
    wanted = [int(x) for x in args.guilds.split(",")]
    guild_by = {g["guildid"]: g for g in companies(e)}
    guilds = [guild_by[gid] for gid in wanted if gid in guild_by]
    lanes = guild_lanes()
    judge = fleet.Judge()
    rng = random.Random(args.seed)
    for i, g in enumerate(guilds):
        lane = lanes[i % len(lanes)]
        asks = [(band, band_lands(zones, band, g["faction"]), CHOICES) for band in BANDS]
        try:
            picks = propose(lane, fleet.MAX_ATTEMPTS, e, judge, g, asks)
        except Exception as ex:
            print(f"== {lane.name} seats for {g['name']}: FAILED {ex}", flush=True)
            continue
        print(f"== {lane.name} seats for {g['name']} ({g['faction']})")
        for band, chosen in picks.items():
            for zone, hold in chosen:
                print(f"   {band:9} {zone[1]:22} {hold}")
    if len(guilds) >= 2:
        # The sample pair is treated as sharing one contested land, whatever seats they would get.
        ga, gb = guilds[0], guilds[1]
        weights = DRAW_WEIGHTS[(ga["faction"] == gb["faction"], True)]
        disposition = args.disposition or rng.choices(list(weights), weights=list(weights.values()))[0]
        common = [rng.choice([z for z in zones if z[4] == "C"])]
        lane = lanes[-1]
        try:
            r = build_relation(lane, fleet.MAX_ATTEMPTS, e, judge, ga, gb, disposition, common)
        except Exception as ex:
            print(f"== {lane.name} relation: FAILED {ex}")
            return 1
        print(f"== {lane.name} relation {ga['name']} / {gb['name']}: {disposition} (sharing {common[0][1]})")
        print(f"   origin: {r['origin']}")
        for m in r["moments"]:
            print(f"   - {m}")
        print(f"   {ga['name']} say: {r['a_says']}\n   {gb['name']} say: {r['b_says']}")
    return 0


def show(args):
    ensure_schema()
    names = {int(r[0]): r[1] for r in rg.sql("SELECT guildid, name FROM guild")}
    only = None
    if args.name:
        only = next((gid for gid, n in names.items() if n.lower() == args.name.lower()), None)
        if only is None:
            sys.exit(f"no company named {args.name}")
    seats = {}
    for gid, band, zone, hold in rg.sql(f"SELECT guildid, band, zone_name, {flat('hold')} FROM guild_seat "
                                        "ORDER BY guildid, FIELD(band, 'recruits', 'blooded', 'seasoned')"):
        seats.setdefault(int(gid), []).append((band, zone, hold))
    for gid, rows in seats.items():
        if only not in (None, gid):
            continue
        print(f"{names.get(gid, gid)}: " + "; ".join(f"{b} {z}" for b, z, _ in rows))
        if only:
            for b, z, h in rows:
                print(f"   {b:9} {z:22} {h}")
    print()
    for r in rg.sql(f"SELECT guild_a, guild_b, stance, disposition, {flat('origin')}, {flat('a_says')}, "
                    f"{flat('b_says')} FROM guild_relation ORDER BY stance"):
        a, b = int(r[0]), int(r[1])
        if only not in (None, a, b):
            continue
        print(f"{names.get(a, a)} / {names.get(b, b)}: {r[3]} ({float(r[2]):.0f})")
        if only:
            print(f"   {r[4]}\n   {names.get(a)} say: {r[5]}\n   {names.get(b)} say: {r[6]}")
    return 0


# ---------------------------------------------------------------------------
# rank names (plan 18 P4): what a company's own people call its five standings

RANKS_PROMPT = """{setting}

The company {name} serves the {faction}.
Its charter: {charter}
Its history: {history}

Name the five standings in this company, from the one who leads it down to its newest recruit, as its own people call them. These are titles anyone in that place would hold, not descriptions of particular people: a title of one or two words (three at most), at most 18 letters and spaces, fitting this company's character. No possessives, no leading "The". Not the plain words leader, master, officer, veteran, member or initiate; no ranks of the Alliance or Horde armies; no numbers; nothing outside the world.
Other companies already use these titles, so choose none of them: {taken}.
Reply with JSON only: {{"ranks": ["who leads", "second", "third", "fourth", "newest recruit"]}}"""
RANK_TITLE = re.compile(r"^(?!the )[A-Za-z][A-Za-z -]*$", re.I)   # no possessives, no leading "The"
PLAIN_RANKS = {"guild master", "master", "leader", "officer", "veteran", "member", "initiate"}


def build_ranks(lane, attempt, e, judge, g, taken):
    # Member roles are left out: the models turn a particular member's description into a title. Titles other
    # companies hold are named in the prompt: a rejection afterwards alone never broke the models of their favourites.
    prompt = RANKS_PROMPT.format(setting=e["setting"], name=g["name"], faction=g["faction"], charter=g["charter"],
                                 history=g["history"], taken=", ".join(sorted(taken)) or "none yet")
    raw = extract_json(lane.chat([{"role": "user", "content": prompt}], 200, temperature=0.8))
    titles = [clean(t) for t in raw.get("ranks") or [] if isinstance(t, str)]
    if len(titles) != 5 or len({t.lower() for t in titles}) != 5:
        raise ValueError(f"want five different titles: {titles}")
    for t in titles:
        if not RANK_TITLE.match(t) or len(t) > 20 or len(t.split()) > 3 or t.lower() in PLAIN_RANKS:
            raise ValueError(f"unusable title {t!r}")
    vet(" ".join(titles), e, judge, attempt, f"ranks {g['guildid']} {g['name']}")
    return titles


def store_ranks(guildid, titles, lane, e):
    """company_rank_name and guild_rank.rname (shown after a worldserver restart). Also used by founding.py."""
    stmts = ["START TRANSACTION;"]
    for rid, title in enumerate(titles):
        stmts += [f"INSERT INTO company_rank_name (guildid, rid, rname, model, era) VALUES ({guildid}, {rid}, "
                  f"{rg.q(title)}, {rg.q(model_name(lane))}, {rg.q(e['name'])}) ON DUPLICATE KEY UPDATE "
                  "rname = VALUES(rname), model = VALUES(model), era = VALUES(era), generated_at = NOW();",
                  f"UPDATE guild_rank SET rname = {rg.q(title)} WHERE guildid = {guildid} AND rid = {rid};"]
    stmts.append("COMMIT;")
    rg.sql("\n".join(stmts), fetch=False)


def ranks(args):
    e = eras.get(args.era)
    rg.sql(open(os.path.join(HERE, "company_tables.sql")).read(), fetch=False)
    wanted = {int(x) for x in args.guilds.split(",")} if args.guilds else None
    guilds = [g for g in companies(e) if wanted is None or g["guildid"] in wanted]
    named = {int(r[0]) for r in rg.sql("SELECT DISTINCT guildid FROM company_rank_name")}
    if not args.redo and not args.dry:
        guilds = [g for g in guilds if g["guildid"] not in named]
    lanes, judge = guild_lanes(), fleet.Judge()
    log(f"rank names for {len(guilds)} companies; lanes: " + ", ".join(f"{l.name}x{l.slots}" for l in lanes))
    pool = fleet.Pool(lanes, log=log)
    for g in guilds:
        def run(lane, attempt, g=g):
            # The models reuse favourites ("Last Breath" in five companies); each company's titles are its own. The
            # check after is kept: companies named at the same time do not see each other's titles in the prompt.
            held = [r[0] for r in rg.sql(f"SELECT rname FROM company_rank_name WHERE guildid <> {g['guildid']}")]
            taken = {t.lower() for t in held}
            titles = build_ranks(lane, attempt, e, judge, g, held)
            clash = [t for t in titles if t.lower() in taken or t.lower() == g["name"].lower()]
            if clash:
                raise ValueError(f"titles held by another company, or the company's own name: {clash}")
            if args.dry:
                print(f"{g['name']}: " + " / ".join(titles), flush=True)
                return
            store_ranks(g["guildid"], titles, lane, e)
            log(f"ranks for {g['name']}: " + " / ".join(titles))
        pool.submit("guild", 0, run, f"ranks {g['name']}")
    failures = pool.run()
    for j in failures:
        log(f"FAILED {j['label']}: {' | '.join(j['errors'])}")
    return 1 if failures else 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("ranks", help="in-world names for each company's five standings (plan 18 P4)")
    k.add_argument("--era", required=True)
    k.add_argument("--guilds", help="guild ids, e.g. 6,9 (default: every company with lore in the era)")
    k.add_argument("--redo", action="store_true", help="also rename companies already named")
    k.add_argument("--dry", action="store_true", help="print the titles, store nothing")
    s = sub.add_parser("seed", help="seats and relations for every company with lore in the era")
    s.add_argument("--era", required=True)
    s.add_argument("--reseed", action="store_true", help="clear the era's seats and relations first")
    s.add_argument("--force", action="store_true", help="with --reseed, also discard relations moved by incidents")
    s.add_argument("--seed", type=int, default=None, help="random seed for the draws")
    p = sub.add_parser("sample", help="seat choices for a few companies and one relation; stores nothing")
    p.add_argument("--era", required=True)
    p.add_argument("--guilds", required=True, help="guild ids, e.g. 6,9")
    p.add_argument("--disposition", choices=list(DISPOSITIONS))
    p.add_argument("--seed", type=int, default=None)
    w = sub.add_parser("show", help="seats and relations")
    w.add_argument("name", nargs="?")
    args = ap.parse_args()
    return {"seed": seed, "sample": sample, "show": show, "ranks": ranks}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
