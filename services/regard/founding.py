#!/usr/bin/env python3
"""Founding: a company raised by a real player's charter gets its story.

custom wow plans/18, step P5. regard.py notices a guild with no lore and a real player at its head and starts
`founding.py once` in the background (one run at a time: founding.lock). For each such company, in order, recording
the last finished step in company_founding so a failed run picks up where it stopped:

  lore       history, charter, motto and member roles on a guild lane, drawn from what the founder has done in the
             last two weeks, why each signer followed them (their regard for the founder) and, when most of the first
             members once rode with a company that broke apart, that company's story: the new one is its heir.
             Vetted for the era.
  ranks      the company's five standings, distinct from every other company's (rivalry.build_ranks).
  relations  with each company the first members feel strongly about, that an heir inherits, or that keeps a seat or
             holds land where the charter was signed or where the founder has done most. Stance from the members'
             mean regard toward its people, plus half an heir's old stance; words of how it stands on a guild lane,
             except between strangers.
  word       incident 'founded' for the company and for each neighbour, a line in each member's story about the
             charter (lore_character_note, projected into the bios), and guild.info / guild.motd. Bios and guild text
             show after a worldserver restart.

It keeps no seat (operator decision G4): its first land comes through influence, as any company's does.
Standing rule: bots are people living in Azeroth. Everything written is in-world.

Usage:
  python3 founding.py once [--era classic]              every company due (what regard.py starts)
  python3 founding.py once --guild N [--force]          one company; --force gives a failed one fresh attempts
  python3 founding.py sample --guild N [--era classic]  the lore, ranks and relations it would write, storing
                                                        nothing (works on any company, e.g. a seeded one, for a look)
Kill switch: /opt/wow/regard/NO_FOUNDING (regard.py starts no runs).
"""
import argparse
import collections
import fcntl
import json
import os
import sys

import regard as rg  # also puts /opt/wow/lore on the path
import era as eras  # noqa: E402
import fleet  # noqa: E402
import gen_backstories as gb  # noqa: E402
import rivalry  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
log = rg.log
sql, q = rg.sql, rg.q

STEPS = ["pending", "lore", "ranks", "relations", "done"]   # company_founding.state: the last step finished
DEEDS_DAYS = 14
HEIR_DAYS = 60               # a company that broke apart this recently can have an heir ...
HEIR_SHARE = 0.5             # ... which inherits its stances at this share
RELATION_FEELING = 8.0       # the first members' mean regard toward a company's people this strong makes a relation
RELATIONS_MAX = 6
CAPITALS = {1519: "Stormwind", 1537: "Ironforge", 1657: "Darnassus", 1637: "Orgrimmar", 1638: "Thunder Bluff",
            1497: "the Undercity"}

FOUNDING_PROMPT = """You are a loremaster for {setting} Everything you write is real history to the people in it. Never mention games, players, levels as numbers, raids, loot, servers or anything outside the world.

A new {faction} company called "{name}" has just been founded. {founder_line} put a charter before the people below, and they signed it {place}. Treat the name as given: if it sounds odd or comic, explain in-world how the company came to be called that (a nickname that stuck, a joke among the founders, a boast).

What {founder} has done these past two weeks:
{deeds}

The first members, and why each followed {founder}:
{roster}
{heir}
Reply with ONLY a JSON object, no markdown fences:
{{
  "history": "150 to 230 words: how {founder} and these first members came together, why they signed, what the company means to do, and {guild_now}. It was founded only days ago: it has no great victories and holds no land yet. Keep to the deeds and reasons given; invent no battles or famous deeds.",
  "charter": "the company's public description as it would be posted in an inn or muster hall, under 450 characters",
  "motto": "the founder's first word to the company, under 110 characters",
  "member_roles": {{"<each member name exactly as given>": "one sentence on their place or promise in the company"}}
}}"""

HEIR_BLOCK = """
Most of these first members once rode with {heir}, a company that broke apart not long ago. Its story was:
{history}
The new company carries on what was left of it, in its own way.
"""


def flat(col):
    return f"REPLACE(REPLACE(COALESCE({col}, ''), CHAR(10), ' '), CHAR(9), ' ')"


def place_of(zone):
    if zone in rg.LAND:
        return "in " + rg.land_name(zone)
    if zone in CAPITALS:
        return "in " + CAPITALS[zone]
    return "on the road"


def words_for_count(n):
    return "a great many foes" if n >= 200 else "many foes" if n >= 50 else "some foes" if n else "few foes"


def context(gid, e):
    """Everything the steps draw on. Works for any company; the founder is whoever leads it."""
    row = sql(f"SELECT leaderguid, createdate, name FROM guild WHERE guildid = {gid}")
    if not row:
        raise LookupError(f"no guild {gid}")
    leader, created, name = int(row[0][0]), int(row[0][1]), "\t".join(row[0][2:])
    who = sql(f"SELECT name, race, class, gender, level FROM characters WHERE guid = {leader}")[0]
    founder = dict(guid=leader, name=who[0], race=int(who[1]), cls=int(who[2]), gender=int(who[3]), level=int(who[4]))
    faction = "Horde" if founder["race"] in rg.HORDE else "Alliance"
    members = [m for m in gb.roster(gid, e) if m["guid"] != leader]

    pattern = '{"guild":%d,%%' % gid
    found = sql(f"SELECT zone_id FROM ledger_event WHERE event_type = 'guild_found' AND actor_guid = {leader} "
                f"AND detail LIKE {q(pattern)} ORDER BY id DESC LIMIT 1")
    zone = int(found[0][0]) if found else 0

    reasons = {}
    description, last_reason = flat("NULLIF(description, '')"), flat("last_reason")
    if members:
        for r in sql(f"SELECT bot_guid, {description}, {last_reason} FROM regard "
                     f"WHERE other_guid = {leader} AND bot_guid IN ({','.join(str(m['guid']) for m in members)})"):
            reasons[int(r[0])] = r[1] or r[2]

    lands, foes, errands, kills = collections.Counter(), [], [], 0
    for kind, z, m, detail in sql(f"SELECT event_type, zone_id, map_id, {flat('detail')} FROM ledger_event "
                                  f"WHERE actor_guid = {leader} AND ts > NOW() - INTERVAL {DEEDS_DAYS} DAY "
                                  "AND event_type IN ('kill', 'quest_complete', 'zone_change') ORDER BY id DESC LIMIT 5000"):
        land = rg.land_of(int(z), int(m))
        if land:
            lands[land] += 1
        try:
            d = json.loads(detail) if detail.startswith("{") else {}
        except ValueError:
            d = {}
        if kind == "kill":
            kills += 1
            if (int(d.get("rank") or 0) or d.get("boss")) and d.get("name") and d["name"] not in foes:
                foes.append(str(d["name"])[:60])
        elif kind == "quest_complete" and d.get("title") and d["title"] not in errands:
            errands.append(str(d["title"])[:60])
    deed_lands = [z for z, _ in lands.most_common(3)]
    deeds = []
    if deed_lands:
        deeds.append("- Travelled most in " + rg.join_names([rg.land_name(z) for z in deed_lands]))
    deeds.append(f"- Fought {words_for_count(kills)}")
    if foes:
        deeds.append("- Notable foes brought down: " + ", ".join(foes[:6]))
    if errands:
        deeds.append("- Errands seen through: " + "; ".join(errands[:6]))

    heir = None
    guids = {m["guid"] for m in members}
    for eid, dead_gid, roll, dead_name in sql("SELECT ended_id, guildid, members, name FROM company_ended "
                                             f"WHERE ended_at > NOW() - INTERVAL {HEIR_DAYS} DAY ORDER BY ended_id DESC"):
        were = {int(x[0]) for x in json.loads(roll)} & guids
        if len(were) >= 3 and 2 * len(were) >= len(guids):
            archived = flat("JSON_UNQUOTE(JSON_EXTRACT(data, '$.history'))")
            history = sql(f"SELECT {archived} FROM company_archive WHERE ended_id = {eid} AND source = 'lore_guild' LIMIT 1")
            heir = dict(ended_id=int(eid), guildid=int(dead_gid), name=dead_name, were=were,
                        history=history[0][0] if history else "")
            break

    return dict(guildid=gid, name=name, created=created, founder=founder, faction=faction, members=members,
                zone=zone, place=place_of(zone), reasons=reasons, deeds=deeds, deed_lands=deed_lands, heir=heir)


def lore_prompt(ctx, e):
    f = ctx["founder"]
    race, cls = rg.RACES.get(f["race"], "wanderer"), rg.CLASSES.get(f["cls"], "adventurer")
    founder_line = f"{f['name']}, a {'female' if f['gender'] else 'male'} {race} {cls},"
    roster = []
    for m in ctx["members"]:
        why = ctx["reasons"].get(m["guid"]) or f"they had heard well of {f['name']}"
        roster.append(f"- {m['name']}: {'female' if m['gender'] else 'male'} {rg.RACES.get(m['race'], 'wanderer')} "
                      f"{rg.CLASSES.get(m['cls'], 'adventurer')}, {eras.standing(m['level'], e)}. In their own words: {why}")
    heir = HEIR_BLOCK.format(heir=ctx["heir"]["name"], history=ctx["heir"]["history"]) if ctx["heir"] and ctx["heir"]["history"] else ""
    return FOUNDING_PROMPT.format(setting=e["setting"], faction=ctx["faction"], name=ctx["name"], founder_line=founder_line,
                                  place=ctx["place"], founder=f["name"], deeds="\n".join(ctx["deeds"]),
                                  roster="\n".join(roster) or "- (none named)", heir=heir, guild_now=e["guild_now"])


def write_lore(lane, attempt, e, judge, ctx, store):
    data = gb.extract_json(lane.chat([{"role": "user", "content": lore_prompt(ctx, e)}], 1400, temperature=0.85))
    history, charter, motto = (rivalry.clean(data.get(k)) for k in ("history", "charter", "motto"))
    names = {m["name"] for m in ctx["members"]} | {ctx["founder"]["name"]}
    roles = {k: rivalry.clean(v) for k, v in (data.get("member_roles") or {}).items() if isinstance(v, str) and k in names}
    if not 110 <= len(history.split()) <= 280:
        raise ValueError(f"history has {len(history.split())} words")
    if not charter or not motto:
        raise ValueError("charter or motto missing")
    rivalry.vet(" ".join([history, charter, motto, *roles.values()]), e, judge, attempt,
                f"founding {ctx['guildid']} {ctx['name']}")
    if store:
        sql("INSERT INTO lore_guild (guildid, history, charter, motto, member_roles, model, era) VALUES "
            f"({ctx['guildid']}, {q(history)}, {q(charter[:500])}, {q(motto[:128])}, {q(json.dumps(roles))}, "
            f"{q(rivalry.model_name(lane))}, {q(e['name'])}) ON DUPLICATE KEY UPDATE history = VALUES(history), "
            "charter = VALUES(charter), motto = VALUES(motto), member_roles = VALUES(member_roles), "
            "model = VALUES(model), era = VALUES(era), generated_at = NOW()", fetch=False)
    return dict(history=history, charter=charter[:500], motto=motto[:128], roles=roles)


def stored_lore(gid):
    r = sql(f"SELECT {flat('history')}, {flat('charter')}, {flat('motto')} FROM lore_guild WHERE guildid = {gid}")
    return dict(history=r[0][0], charter=r[0][1], motto=r[0][2]) if r else None


def write_ranks(lane, attempt, e, judge, ctx, lore, store):
    held = [r[0] for r in sql(f"SELECT rname FROM company_rank_name WHERE guildid <> {ctx['guildid']}")]
    g = dict(guildid=ctx["guildid"], name=ctx["name"], faction=ctx["faction"], charter=lore["charter"],
             history=lore["history"])
    titles = rivalry.build_ranks(lane, attempt, e, judge, g, held)
    taken = {t.lower() for t in held}
    clash = [t for t in titles if t.lower() in taken or t.lower() == ctx["name"].lower()]
    if clash:
        raise ValueError(f"titles held by another company, or the company's own name: {clash}")
    # The sample on a seeded company gave its leader's name as the first title.
    people = {p["name"].lower() for p in ctx["members"]} | {ctx["founder"]["name"].lower()}
    named = [t for t in titles if any(word.lower() in people for word in t.replace("-", " ").split())]
    if named:
        raise ValueError(f"titles naming a person: {named}")
    if store:
        rivalry.store_ranks(ctx["guildid"], titles, lane, e)
    return titles


def disposition(stance):
    if stance <= -55:
        return "enemies"
    if stance <= -25:
        return "rivals"
    if stance <= -8:
        return "wary"
    if stance >= 30:
        return "allies"
    if stance >= 10:
        return "respect"
    return "strangers"


def relation_plan(ctx):
    """[(other guild, stance, disposition, shared land ids)], strongest first."""
    gid = ctx["guildid"]
    feel = {int(r[0]): (float(r[1]), int(r[2])) for r in sql(
        "SELECT m2.guildid, AVG(r.score), COUNT(*) FROM regard r "
        f"JOIN guild_member m1 ON m1.guid = r.bot_guid AND m1.guildid = {gid} "
        f"JOIN guild_member m2 ON m2.guid = r.other_guid AND m2.guildid <> {gid} GROUP BY m2.guildid")}
    lands = sorted({z for z in [ctx["zone"]] + ctx["deed_lands"][:2] if z in rg.LAND})
    placed = collections.defaultdict(set)   # other guild -> lands it keeps or holds among ours
    if lands:
        zl = ",".join(map(str, lands))
        for g, z in sql(f"SELECT guildid, zone_id FROM guild_seat WHERE zone_id IN ({zl}) "
                        f"UNION SELECT holder, zone_id FROM guild_holding WHERE zone_id IN ({zl})"):
            placed[int(g)].add(int(z))
    inherited = {}
    if ctx["heir"]:
        dead = ctx["heir"]["guildid"]
        for a, b, stance in sql("SELECT JSON_EXTRACT(data, '$.guild_a'), JSON_EXTRACT(data, '$.guild_b'), "
                                f"JSON_EXTRACT(data, '$.stance') FROM company_archive WHERE ended_id = {ctx['heir']['ended_id']} "
                                "AND source = 'guild_relation'"):
            other = int(b) if int(a) == dead else int(a)
            inherited[other] = inherited.get(other, 0.0) + float(stance) * HEIR_SHARE
    living = rg.company_names()
    strong = {g for g, (avg, n) in feel.items() if abs(avg) >= RELATION_FEELING and n >= 2}
    plan = []
    for other in (set(placed) | set(inherited) | strong):
        if other == gid or other not in living:
            continue
        stance = round(max(-70.0, min(45.0, feel.get(other, (0.0, 0))[0] + inherited.get(other, 0.0))), 1)
        plan.append((other, stance, disposition(stance), sorted(placed.get(other, ()))))
    plan.sort(key=lambda p: (-abs(p[1]), p[0]))
    return plan[:RELATIONS_MAX]


def store_relation(a, b, stance, disp, zones, words, model, era):
    words = words or dict(origin="", moments=[], a_says="", b_says="")
    sql("INSERT INTO guild_relation (guild_a, guild_b, stance, disposition, shared_zones, origin, moments, a_says, b_says, "
        f"model, era) VALUES ({a}, {b}, {stance}, {q(disp)}, {q(json.dumps(zones))}, {q(words['origin'])}, "
        f"{q(json.dumps(words['moments']))}, {q(words['a_says'][:160])}, {q(words['b_says'][:160])}, {q(model)}, {q(era)}) "
        "ON DUPLICATE KEY UPDATE stance = VALUES(stance), disposition = VALUES(disposition), "
        "shared_zones = VALUES(shared_zones), origin = VALUES(origin), moments = VALUES(moments), "
        "a_says = VALUES(a_says), b_says = VALUES(b_says), model = VALUES(model), era = VALUES(era)", fetch=False)


def write_relations(lanes, e, judge, ctx, lore, store):
    plan = relation_plan(ctx)
    others = {g["guildid"]: g for g in rivalry.companies(e)}
    mine = dict(guildid=ctx["guildid"], name=ctx["name"], faction=ctx["faction"], charter=lore["charter"],
                history=lore["history"])
    pool = fleet.Pool(lanes, log=log)
    written = []
    for other, stance, disp, zones in plan:
        a, b = sorted((ctx["guildid"], other))
        if disp == "strangers" or other not in others:
            if store:
                store_relation(a, b, stance, "strangers", zones, None, "founding", e["name"])
            written.append((other, stance, "strangers", None))
            continue
        ga, gb_ = (others[other], mine) if a == other else (mine, others[other])

        def run(lane, attempt, a=a, b=b, stance=stance, disp=disp, zones=zones, ga=ga, gb_=gb_, other=other):
            words = rivalry.build_relation(lane, attempt, e, judge, ga, gb_, disp, [rg.LAND[z] for z in zones])
            if store:
                store_relation(a, b, stance, disp, zones, words, rivalry.model_name(lane), e["name"])
            written.append((other, stance, disp, words))
        pool.submit("guild", 0, run, f"relation {ga['name']} / {gb_['name']}")
    failures = pool.run()
    if store and written:
        rg.refresh_guild_baselines()
    if failures:
        raise RuntimeError("relations: " + " | ".join(j["label"] for j in failures))
    return written


def write_word(ctx, lore):
    gid, name, founder, place = ctx["guildid"], ctx["name"], ctx["founder"]["name"], ctx["place"]
    living = rg.company_names()
    neighbours = {int(r[0]) for r in sql(f"SELECT IF(guild_a = {gid}, guild_b, guild_a) FROM guild_relation "
                                         f"WHERE {gid} IN (guild_a, guild_b)")}
    if ctx["zone"] in rg.LAND:
        neighbours |= {int(r[0]) for r in sql(f"SELECT guildid FROM guild_seat WHERE zone_id = {ctx['zone']}")}
    neighbours = sorted(g for g in neighbours if g in living and g != gid)
    zone = ctx["zone"] or "NULL"
    stmts = ["START TRANSACTION;",
             f"INSERT INTO guild_incident (guild_a, guild_b, zone_id, kind, detail) VALUES ({gid}, NULL, {zone}, 'founded', "
             f"{q(f'{founder} raised the banner of {name} {place}'[:255])});"]
    for other in neighbours:
        stmts.append(f"INSERT INTO guild_incident (guild_a, guild_b, zone_id, kind, detail) VALUES ({other}, {gid}, {zone}, "
                     f"'founded', {q(f'A new company, {name}, has raised its banner {place} under {founder}'[:255])});")
    heir_guids = ctx["heir"]["were"] if ctx["heir"] else set()
    notes = []
    for m in ctx["members"]:
        note = f"You put your name to {founder}'s charter {place} and have ridden with {name} since its first day."
        if m["guid"] in heir_guids:
            note = note[:-1] + f", carrying on what was left of {ctx['heir']['name']}."
        notes.append((m["guid"], note[:255]))
    for part in rg.chunks(notes, 200):
        stmts.append("INSERT INTO lore_character_note (guid, source, note) VALUES "
                     + ",".join(f"({g}, 'founding', {q(n)})" for g, n in part) + ";")
    stmts += [f"UPDATE guild SET info = {q(lore['charter'][:500])}, motd = {q(lore['motto'][:128])} WHERE guildid = {gid};",
              "COMMIT;"]
    sql("\n".join(stmts), fetch=False)
    gb.project(None)   # bios with the new notes; they load at the next worldserver restart
    return len(neighbours), len(notes)


def set_state(gid, state, detail):
    sql(f"UPDATE company_founding SET state = {q(state)}, detail = {q(str(detail)[:255])} WHERE guildid = {gid}", fetch=False)


def run_one(lanes, fn, label):
    """One lane job through the pool's retries; raises when every attempt failed."""
    result = {}
    pool = fleet.Pool(lanes, log=log)
    pool.submit("guild", 0, lambda lane, attempt: result.__setitem__("value", fn(lane, attempt)), label)
    failures = pool.run()
    if failures:
        raise RuntimeError(f"{label}: " + " | ".join(failures[0]["errors"])[-200:])
    return result["value"]


def found(gid, e, lanes, judge):
    ctx = context(gid, e)
    sql("INSERT INTO company_founding (guildid, founded_at, founder_guid, zone_id, heir_ended_id) VALUES "
        f"({gid}, {ctx['created']}, {ctx['founder']['guid']}, {ctx['zone'] or 'NULL'}, "
        f"{ctx['heir']['ended_id'] if ctx['heir'] else 'NULL'}) ON DUPLICATE KEY UPDATE "
        # A reused id is another company: start over. MySQL applies SET left to right, so founded_at goes last.
        "state = IF(founded_at = VALUES(founded_at), state, 'pending'), "
        "attempts = IF(founded_at = VALUES(founded_at), attempts, 0), "
        "founder_guid = VALUES(founder_guid), zone_id = VALUES(zone_id), heir_ended_id = VALUES(heir_ended_id), "
        "founded_at = VALUES(founded_at); "
        f"UPDATE company_founding SET attempts = attempts + 1, last_attempt = NOW() WHERE guildid = {gid};", fetch=False)
    state = sql(f"SELECT state FROM company_founding WHERE guildid = {gid}")[0][0]
    step = STEPS.index(state) if state in STEPS else 0
    log(f"founding {ctx['name']} (id {gid}) by {ctx['founder']['name']}, {len(ctx['members'])} first members, "
        f"{ctx['place']}{', heir to ' + ctx['heir']['name'] if ctx['heir'] else ''}; from step {state}")
    try:
        lore = stored_lore(gid)
        if step < 1 or not lore:
            lore = run_one(lanes, lambda lane, attempt: write_lore(lane, attempt, e, judge, ctx, True), f"lore {ctx['name']}")
            set_state(gid, "lore", lore["motto"])
        if step < 2:
            titles = run_one(lanes, lambda lane, attempt: write_ranks(lane, attempt, e, judge, ctx, lore, True),
                             f"ranks {ctx['name']}")
            set_state(gid, "ranks", " / ".join(titles))
        if step < 3:
            written = write_relations(lanes, e, judge, ctx, lore, True)
            set_state(gid, "relations", ", ".join(f"{d} {o}" for o, _, d, _ in written) or "none")
        if step < 4:
            told, noted = write_word(ctx, lore)
            set_state(gid, "done", f"{told} neighbours told, {noted} member notes")
        log(f"founding {ctx['name']} done")
    except Exception as ex:
        sql(f"UPDATE company_founding SET detail = {q(f'{type(ex).__name__}: {ex}'[:255])} WHERE guildid = {gid}",
            fetch=False)
        log(f"founding {ctx['name']} stopped: {type(ex).__name__}: {ex}")


def sample(gid, e, lanes, judge):
    ctx = context(gid, e)
    print(f"== {ctx['name']} ({ctx['faction']}), founder {ctx['founder']['name']}, {len(ctx['members'])} members, "
          f"{ctx['place']}{', heir to ' + ctx['heir']['name'] if ctx['heir'] else ''}")
    print("-- prompt --\n" + lore_prompt(ctx, e))
    lore = run_one(lanes, lambda lane, attempt: write_lore(lane, attempt, e, judge, ctx, False), f"lore {ctx['name']}")
    print(f"-- history --\n{lore['history']}\n-- charter --\n{lore['charter']}\n-- motto --\n{lore['motto']}")
    for k, v in list(lore["roles"].items())[:4]:
        print(f"   {k}: {v}")
    titles = run_one(lanes, lambda lane, attempt: write_ranks(lane, attempt, e, judge, ctx, lore, False),
                     f"ranks {ctx['name']}")
    print("-- ranks --\n" + " / ".join(titles))
    names = rg.company_names()
    print("-- relations it would make --")
    for other, stance, disp, zones in relation_plan(ctx):
        print(f"   {names.get(other)}: {disp} ({stance}), sharing {', '.join(rg.LAND[z][1] for z in zones) or 'no land'}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("once", help="found every company due, or one")
    o.add_argument("--era", default=rg.site.get("ERA", "classic"))
    o.add_argument("--guild", type=int)
    o.add_argument("--force", action="store_true", help="with --guild: fresh attempts for a failed company")
    s = sub.add_parser("sample", help="what founding would write for a company; stores nothing")
    s.add_argument("--era", default=rg.site.get("ERA", "classic"))
    s.add_argument("--guild", type=int, required=True)
    args = ap.parse_args()

    e = eras.get(args.era)
    sql(open(os.path.join(HERE, "company_tables.sql")).read(), fetch=False)
    lanes, judge = rivalry.guild_lanes(), fleet.Judge()
    if args.cmd == "sample":
        return sample(args.guild, e, lanes, judge)

    lock = open(rg.FOUNDING_LOCK, "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another founding run holds the lock")
        return 0
    if args.guild:
        if args.force:
            sql(f"UPDATE company_founding SET attempts = 0 WHERE guildid = {args.guild}", fetch=False)
        todo = [args.guild]
    else:
        todo = rg.founding_due()
    for gid in todo:
        found(gid, e, lanes, judge)
    return 0


if __name__ == "__main__":
    sys.exit(main())
