#!/usr/bin/env python3
"""journey_story.py - the story of one journey, written up from what was already recorded.

custom wow plans/53-PLAN-the-story-of-a-journey.md

The Journey view (plan 42) lays one character's whole record out in time. It answers *what happened,
and in what order*. It cannot answer *what it was like*. This does: it cuts the record into the
stretches the character actually lived, hands each one to the chronicler's scribes, and keeps the
prose they write back.

**A journey, here, is a leg**: from the moment they set out until the company broke up or the record
went quiet. There is no logout event in the ledger -- the ten event types are kill, death, revive,
quest_complete, level_up, group_join, group_leave, zone_change, loot_item and pvp_kill, and
`characters.logout_time` keeps only the LATEST logout, never a history -- so a log-off cannot be read
for any past journey. It is inferred from a lull instead, the same forty-five minutes recall.py has
always used to split an outing (`recall.py:304`). A leg therefore ends on one of four things:

    disband   the company broke up
    left      they walked away from it
    kicked    they were put out of it
    quiet     nothing happened for forty-five minutes

Inside a leg, a **bout** is a stretch in one place with no gap longer than half an hour -- the same
cut the page already draws (`journey-full.js:20 CHAPTER_GAP`). One model call per bout, stitched into
one story, because nothing in this project stitches calls and a nineteen-hour night will not fit in a
prompt: the whole of `recall.py` caps a single digest at forty deeds for exactly that reason.

Nothing here scores or decides anything, and nothing here touches a game object. It reads the same
rows the dashboard already shows and writes prose into `journey_story`.

Run:
    journey_story.py legs    --guid 702
    journey_story.py write   --guid 702 --start "2026-09-24 12:44:04"
    journey_story.py publish --guid 702
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time

_SERVICES = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(_SERVICES, "lore"))
sys.path.insert(0, os.path.join(_SERVICES, "regard"))
sys.path.insert(0, _SERVICES)
import era as eras  # noqa: E402
import fleet  # noqa: E402
import regard as rg  # noqa: E402
from common import site  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STORIES_DIR = os.path.join(site.get("DATA_DIR", "/opt/wow/server/data"), "dashboard-data",
                           "journey-stories")

log = rg.log
sql, q = rg.sql, rg.q

# A leg ends on a lull this long. recall.py:304 uses the same forty-five minutes to cut an outing,
# and the two want to agree: a bot's memory of a night and the story of that night should not
# disagree about where the night ended.
LEG_GAP = 45 * 60
CHAPTER_GAP = 1800          # a bout: one place, no gap longer than half an hour (journey-full.js:20)
MIN_DEEDS = 3               # two is an incident, not a journey (recall.py:305)
MAX_DEEDS_PER_CHAPTER = 40  # a long night must not build a prompt the model cannot hold
MAX_CHAPTERS = 8            # past this a journey is summarised, not narrated, and the cost runs away
MAX_TALK_PER_CHAPTER = 14
MAX_ATTEMPTS = 4
CHAPTER_TOKENS = 700
CHAPTER_WORDS = (110, 200)  # asked; the floor checked is 0.6 of the low end, as chronicler does

# The novelist serves no live chat, so it can take a long generation without starving anyone.
STORY_LANES = "zb-novelist,evo-quality,z13-qwen35"

DIGITS = re.compile(r"\d")

# Deeds worth telling. Ordinary kills are excluded and tallied instead: 864,276 of the realm's events
# are kills, and 627,714 of them are rank 0 with no boss flag. Listing them would bury the story.
NOTABLE_KILL = ("(COALESCE(JSON_EXTRACT(detail, '$.rank'), 0) <> 0 "
                "OR COALESCE(JSON_EXTRACT(detail, '$.boss'), 0) = 1)")
LEG_EVENTS = ("death", "quest_complete", "level_up", "loot_item", "zone_change", "pvp_kill",
              "group_join", "group_leave")

RANK_WORDS = {1: "a fearsome foe", 2: "a rare and fearsome creature",
              3: "a great terror of the land", 4: "a rare creature seldom seen"}


def detail_of(text):
    try:
        return json.loads(text or "{}")
    except ValueError:
        return {}


def deed_of(kind, d):
    """What happened, in the world's own words. Returns None for anything not worth a line.

    No figures anywhere: the scribes are checked for digits (check_words), and a deed that hands them
    "level 31" teaches them to write it back.
    """
    name = d.get("name") or ""
    rank = int(d.get("rank") or 0)
    if kind == "kill":
        if d.get("boss") in (1, True, "1", "true") or rank == 3:
            return f"brought down {name}, the master of that place", 10
        # Rank 1 is an ELITE, and inside a dungeon an elite is the trash: 130 of the 279 kills in the
        # one Deadmines run (recall.py:328-330). Feeding them in taught the scribe to recite an
        # inventory -- "a Goblin Craftsman, a Goblin Engineer, a Defias Squallshaper, a Defias
        # Pirate" -- which is the ledger read aloud, not a story. Only the rare and the great count.
        if rank in (2, 4):
            return f"cut down {name}, {RANK_WORDS[rank]}", 6
        return None
    if kind == "death":
        return (f"was struck down by {name}" if name else "was struck down"), 7
    if kind == "quest_complete":
        title = d.get("title") or ""
        return (f"finished the task called '{title}'", 6) if title else None
    if kind == "level_up":
        return "grew stronger", 5
    if kind == "loot_item":
        return (f"took up {name}", 4) if int(d.get("quality") or 0) >= 3 and name else None
    if kind == "pvp_kill":
        return "struck down another of the living", 8
    return None


# ---------------------------------------------------------------------------
# cutting the record into journeys

def _rows_for(guid):
    """Everything this character did, oldest first, with the group comings and goings kept in place."""
    detail_sql = "REPLACE(REPLACE(COALESCE(detail, ''), '\\t', ' '), '\\n', ' ')"
    kinds = ",".join(q(k) for k in LEG_EVENTS)
    rows = sql(f"SELECT UNIX_TIMESTAMP(ts), event_type, zone_id, map_id, {detail_sql} "
               f"FROM ledger_event WHERE actor_guid = {int(guid)} "
               f"AND (event_type IN ({kinds}) OR (event_type = 'kill' AND {NOTABLE_KILL})) "
               "ORDER BY id")
    out = []
    for r in rows:
        out.append(dict(ts=float(r[0]), kind=r[1], zone=int(r[2]), map=int(r[3]),
                        d=detail_of("\t".join(r[4:]))))
    return out


def legs(guid):
    """Every journey this character has lived, newest first.

    The boundary rule, chosen by the operator: a company breaking up ends a journey, and so does a
    forty-five minute lull. `group_join` does not START one -- a leg begins at the first thing they
    did after the last one ended, because riding out alone and being joined ten minutes later is one
    journey, not two.
    """
    rows = _rows_for(guid)
    out, cur = [], None

    def close(leg, ended, at):
        leg["end"] = at
        leg["ended"] = ended
        # Deeds are what makes a journey worth telling; group rows alone are not a journey.
        if leg["deeds"] >= MIN_DEEDS:
            out.append(leg)

    for r in rows:
        if cur and r["ts"] - cur["last"] > LEG_GAP:
            close(cur, "quiet", cur["last"])
            cur = None
        if cur is None:
            cur = dict(start=r["ts"], last=r["ts"], end=r["ts"], ended="quiet", events=[], deeds=0)
        cur["last"] = r["ts"]
        if r["kind"] == "group_leave":
            d = r["d"]
            ended = "disband" if d.get("disband") else ("kicked" if d.get("method") == 1 else "left")
            close(cur, ended, r["ts"])
            cur = None
            continue
        if r["kind"] == "group_join":
            cur["events"].append(r)
            continue
        cur["events"].append(r)
        if deed_of(r["kind"], r["d"]):
            cur["deeds"] += 1
    if cur:
        close(cur, "quiet", cur["last"])

    out.sort(key=lambda l: -l["start"])
    return out


def bouts(leg, place_of):
    """One place, no gap longer than half an hour. The same cut the page draws down the spine."""
    out, cur = [], None
    for e in leg["events"]:
        place = place_of(e)
        if cur is None or e["ts"] - cur["to"] > CHAPTER_GAP or (place and cur["place"] and place != cur["place"]):
            cur = dict(place=place, frm=e["ts"], to=e["ts"], events=[])
            out.append(cur)
        cur["events"].append(e)
        cur["to"] = e["ts"]
        if not cur["place"]:
            cur["place"] = place
    # A bout with nothing worth telling is not a chapter; it is the walk between two of them.
    return [b for b in out if any(deed_of(e["kind"], e["d"]) for e in b["events"])]


# ---------------------------------------------------------------------------
# what the scribe is given

def _talk_in(guid, frm, to, company):
    """What was said in the party during a bout, as that character lived it."""
    guids = ",".join(str(int(g)) for g in sorted({int(guid)} | set(company)))
    rows = sql("SELECT UNIX_TIMESTAMP(ts), speaker_guid, "
               "REPLACE(REPLACE(text, '\\t', ' '), '\\n', ' ') FROM ledger_chat "
               f"WHERE chat_type IN (2, 51) AND speaker_guid IN ({guids}) "
               f"AND ts >= FROM_UNIXTIME({frm - 60}) AND ts <= FROM_UNIXTIME({to + 60}) ORDER BY id")
    return [(int(r[1]), "\t".join(r[2:])) for r in rows]


def gather(guid, leg, who, parties, place_of):
    """The journey as reports: chapters, each with its place, its company, its deeds and its talk."""
    company = set()
    for e in leg["events"]:
        company |= parties.standing_with(guid, e["ts"])
    company.discard(int(guid))

    chapters = []
    for b in bouts(leg, place_of)[:MAX_CHAPTERS]:
        here = set()
        for e in b["events"]:
            here |= parties.standing_with(guid, e["ts"])
        here.discard(int(guid))

        deeds = []
        for e in b["events"]:
            said = deed_of(e["kind"], e["d"])
            if said:
                deeds.append(dict(text=said[0], weight=said[1], ts=e["ts"]))
        deeds = deeds[-MAX_DEEDS_PER_CHAPTER:]

        lines = _talk_in(guid, b["frm"], b["to"], here)[-MAX_TALK_PER_CHAPTER:]
        chapters.append(dict(place=b["place"] or "the wilds", frm=b["frm"], to=b["to"],
                             company=sorted(here), deeds=deeds,
                             talk=[(who.get(g, {}).get("name", "someone"), t) for g, t in lines]))
    return company, chapters


def names_in(guid, company, chapters, who):
    """Every name the judge should let pass: the people, the foes and the places of this journey.

    Without it the small judge flags the realm's own people as inventions -- plan 19 paid for this
    once already, when qwen3-8b called the player companies out of lore on most watches of day one.
    """
    known = {who.get(int(guid), {}).get("name", "")}
    known |= {who.get(g, {}).get("name", "") for g in company}
    for c in chapters:
        known.add(c["place"])
        for d in c["deeds"]:
            known.update(re.findall(r"[A-Z][\w']+(?: [A-Z][\w']+)*", d["text"]))
        for name, _ in c["talk"]:
            known.add(name)
    return {n for n in known if n}


# ---------------------------------------------------------------------------
# writing

def attempt_on(lanes, attempt):
    usable = [lane for lane in lanes if lane.available()] or lanes
    return usable[(attempt - 1) % len(usable)]


def check_words(text, e):
    bad = e["meta"].search(text)
    if bad:
        raise ValueError(f"out-of-world word: {bad.group(0)!r}")
    if DIGITS.search(text):
        raise ValueError("figures in the text")


def clean(text):
    lines = [ln for ln in text.strip().strip('"').splitlines()
             if not ln.lstrip().startswith(("#", "**", "Title:", "Chapter"))]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def chapter_prompt(e, subject, chapter, who, first, last, ended):
    deeds = "\n".join(f"- {d['text']}" for d in chapter["deeds"])
    company = rg.join_names([who.get(g, {}).get("name", "someone") for g in chapter["company"]]) \
        if chapter["company"] else ""
    talk = "\n".join(f"- {name} said: {text}" for name, text in chapter["talk"])
    when = ("This is how the journey began. " if first else
            "This is how the journey ended. " if last else "")
    closing = {
        "disband": "The company broke up at the end of it.",
        "left": f"{subject} walked away from the company at the end of it.",
        "kicked": f"{subject} was put out of the company at the end of it.",
        "quiet": "It simply ended; they did no more that day.",
    }[ended] if last else ""

    return (
        f"You are a scribe who writes up what travellers did, for the people who did it. "
        f"The world you live in: {e['setting']}\n\n"
        f"{when}Write this part of {subject}'s journey. It happened in {chapter['place']}"
        + (f", and they had {company} beside them" if company else ", and they were alone")
        + ".\n\n"
        f"What they did, in order:\n{deeds}\n\n"
        + (f"What was said while they did it:\n{talk}\n\n" if talk else "")
        + (f"{closing}\n\n" if closing else "")
        + f"Write it in plain prose of about {CHAPTER_WORDS[0]} to {CHAPTER_WORDS[1]} words, one or "
        f"two paragraphs, telling it as something that happened to real people in a real place. "
        f"Name {subject} and the people named above. Use only what is written here: invent no other "
        "deeds, no other people and no other places.\n"
        # Without this the scribe dutifully works through every line it was handed and the result is
        # the ledger read aloud. What is wanted is the hour as the people in it would remember it.
        "Tell it, do not list it: never work through the creatures one after another. Say what the "
        "place was like, what it cost them, and what passed between them. Two or three things "
        "deserve naming; the rest is the weather of the day.\n"
        f"The people named here all live, {subject} included: one who fell was carried out, not "
        "lost. No figures or digits (say \"a handful\", \"scores\"), no heading, no title, no lists "
        "and no markdown.")


def write_chapter(prompt, e, known, lanes, judge, step=None):
    messages = [{"role": "user", "content": prompt}]
    errors = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        lane = attempt_on(lanes, attempt)
        try:
            body = clean(lane.chat(messages, CHAPTER_TOKENS, temperature=0.85))
            check_words(body, e)
            if len(body.split()) < int(CHAPTER_WORDS[0] * 0.6):
                raise ValueError("too short")
            flag = judge.check(eras.judge_prompt(e, body, known))
            if flag and attempt < MAX_ATTEMPTS:
                raise ValueError(f"judge: {flag}")
            return body, lane.model, flag
        except Exception as ex:                  # noqa: BLE001 - a lane that will not answer is not fatal
            errors.append(f"{lane.name}: {ex}")
            if step:
                step(f"That telling did not hold ({ex}). Trying another voice.")
            log(f"chapter attempt {attempt} on {lane.name} failed: {ex}")
    raise RuntimeError("; ".join(errors))


def title_of(subject, chapters, ended):
    """A plain title. Not a model call: a heading is not worth a generation, and a model asked for
    one writes a figure into it about a third of the time."""
    places = [c["place"] for c in chapters if c["place"]]
    where = places[0] if places else "the wilds"
    if ended == "disband":
        return f"{subject} in {where}, until the company broke up"
    if ended == "kicked":
        return f"{subject} in {where}, until they were put out"
    return f"{subject} in {where}"


def write_story(guid, leg_start, era="classic", step=None, lanes=None, judge=None):
    """The whole story of one journey. Returns the row that was stored."""
    step = step or (lambda said: None)
    e = eras.get(era)
    guid = int(guid)

    all_legs = legs(guid)
    leg = next((l for l in all_legs if abs(l["start"] - leg_start) < 1.5), None)
    if not leg:
        raise ValueError("There is no journey that began at that moment.")

    parties = rg.Parties()
    known_areas = _areas()

    def place_of(ev):
        return rg.instance_name(ev["map"]) or known_areas.get(ev["zone"]) or ""

    who = rg.people({guid} | {g for ev in leg["events"] for g in parties.standing_with(guid, ev["ts"])})
    subject = who.get(guid, {}).get("name") or f"#{guid}"
    company, chapters = gather(guid, leg, who, parties, place_of)
    if not chapters:
        raise ValueError("Nothing happened in that journey that is worth telling.")

    who.update(rg.people(company))
    known = names_in(guid, company, chapters, who)
    lanes = lanes or fleet.prefer_lanes(STORY_LANES)
    judge = judge or fleet.Judge(slots=1)

    step(f"{subject}'s journey falls into {len(chapters)} "
         f"{'part' if len(chapters) == 1 else 'parts'}. Writing them.")

    written, models, flags = [], [], []
    for i, c in enumerate(chapters):
        step(f"Part {i + 1} of {len(chapters)}: {c['place']}.")
        prose, model, flag = write_chapter(
            chapter_prompt(e, subject, c, who, i == 0, i == len(chapters) - 1, leg["ended"]),
            e, known, lanes, judge, step)
        written.append(dict(place=c["place"], frm=c["frm"], to=c["to"], prose=prose,
                            company=[who.get(g, {}).get("name", "someone") for g in c["company"]]))
        models.append(model)
        flags.append(flag)

    body = "\n\n".join(w["prose"] for w in written)
    title = title_of(subject, chapters, leg["ended"])
    facts = [d["text"] for c in chapters for d in c["deeds"]]
    place = max({c["place"] for c in chapters},
                key=lambda p: sum(len(c["deeds"]) for c in chapters if c["place"] == p))

    store(guid, leg, title, body, written, facts, company, place, len(facts),
          models, era, next((f for f in flags if f), None))
    step(f"Kept. {len(body.split())} words in {len(written)} "
         f"{'part' if len(written) == 1 else 'parts'}.")
    publish(guid)
    return dict(guid=guid, title=title, body=body, chapters=written, words=len(body.split()),
                start=leg["start"], end=leg["end"], ended=leg["ended"], place=place)


def _areas():
    """Zone names. The dashboard cannot name a dungeon interior from /worldmap, so regard.py's
    AreaTable reader is the only complete source on the box (plan 42 §4)."""
    try:
        sys.path.insert(0, os.path.join(_SERVICES, "regard"))
        import areas
        known = dict(areas.load())
    except Exception:                            # noqa: BLE001 - names are a nicety, not a requirement
        known = {}
    for zone, row in rg.LAND.items():
        known[zone] = row[1]
    return known


def main_of(guid):
    rows = sql("SELECT COALESCE(pk.main_guid, 0) FROM person_kind pk "
               f"WHERE pk.guid = {int(guid)}")
    return int(rows[0][0]) if rows and rows[0][0] else 0


def store(guid, leg, title, body, chapters, facts, company, place, deeds, models, era, flag):
    """One row per journey. A regeneration replaces it and counts the version up, so writing it
    again is not a second story saying the same things in other words."""
    sql(open(os.path.join(HERE, "journey_tables.sql")).read(), fetch=False)
    start = dt.datetime.fromtimestamp(leg["start"]).strftime("%Y-%m-%d %H:%M:%S")
    end = dt.datetime.fromtimestamp(leg["end"]).strftime("%Y-%m-%d %H:%M:%S")
    model = ",".join(sorted(set(models)))[:64]
    sql(f"""INSERT INTO journey_story
            (guid, main_guid, leg_start, leg_end, ended, title, body, chapters, facts, companions,
             place, deeds, words, model, era, flag)
            VALUES ({int(guid)}, {main_of(guid)}, {q(start)}, {q(end)}, {q(leg['ended'])},
                    {q(title)}, {q(body)}, {q(json.dumps(chapters))}, {q(json.dumps(facts))},
                    {q(json.dumps(sorted(int(g) for g in company)))}, {q(place)}, {int(deeds)},
                    {int(len(body.split()))}, {q(model)}, {q(era)},
                    {q(flag) if flag else 'NULL'})
            ON DUPLICATE KEY UPDATE
                leg_end = VALUES(leg_end), ended = VALUES(ended), title = VALUES(title),
                body = VALUES(body), chapters = VALUES(chapters), facts = VALUES(facts),
                companions = VALUES(companions), place = VALUES(place), deeds = VALUES(deeds),
                words = VALUES(words), model = VALUES(model), era = VALUES(era),
                flag = VALUES(flag), version = version + 1, created_at = CURRENT_TIMESTAMP;""",
        fetch=False)


def stories_for(guid):
    """Every story kept for this character, newest first. HEX because the mysql CLI is asked for
    --raw output and splits rows on newlines otherwise (plan 19's gotcha)."""
    sql(open(os.path.join(HERE, "journey_tables.sql")).read(), fetch=False)
    rows = sql("SELECT UNIX_TIMESTAMP(leg_start), UNIX_TIMESTAMP(leg_end), ended, HEX(title), "
               "HEX(body), HEX(chapters), HEX(facts), HEX(companions), HEX(place), deeds, words, "
               "model, era, version, UNIX_TIMESTAMP(created_at), COALESCE(HEX(flag), '') "
               f"FROM journey_story WHERE guid = {int(guid)} ORDER BY leg_start DESC")

    def un(h):
        return bytes.fromhex(h).decode("utf-8") if h else ""

    out = []
    for r in rows:
        out.append(dict(start=float(r[0]), end=float(r[1]), ended=r[2], title=un(r[3]),
                        body=un(r[4]), chapters=json.loads(un(r[5]) or "[]"),
                        facts=json.loads(un(r[6]) or "[]"),
                        companions=json.loads(un(r[7]) or "[]"), place=un(r[8]),
                        deeds=int(r[9]), words=int(r[10]), model=r[11], era=r[12],
                        version=int(r[13]), written_at=float(r[14]), flag=un(r[15])))
    return out


def publish(guid):
    """What the page reads. Its own directory, never `journeys/`: write_journeys sweeps every file
    in there whose guid is not in the set it just built, so a story left beside one would be
    deleted within ten minutes (regard.py:2395-2401)."""
    os.makedirs(STORIES_DIR, exist_ok=True)
    doc = dict(guid=int(guid), generated=time.time(), stories=stories_for(guid))
    path = os.path.join(STORIES_DIR, f"{int(guid)}.json")
    with open(path + ".tmp", "w") as f:
        json.dump(doc, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    return len(doc["stories"])


def legs_doc(guid):
    """The journeys this character has lived, and which of them have been written up. This is what
    the gate answers a page with, so it can offer to write the ones that have no story yet."""
    parties = rg.Parties()
    known_areas = _areas()
    have = {round(s["start"]): s for s in stories_for(guid)}
    out = []
    for leg in legs(guid):
        company = set()
        for e in leg["events"]:
            company |= parties.standing_with(guid, e["ts"])
        company.discard(int(guid))
        who = rg.people(company)
        places = [rg.instance_name(e["map"]) or known_areas.get(e["zone"]) or "" for e in leg["events"]]
        places = [p for p in places if p]
        told = have.get(round(leg["start"]))
        out.append(dict(
            start=leg["start"], end=leg["end"], ended=leg["ended"], deeds=leg["deeds"],
            place=max(set(places), key=places.count) if places else "the wilds",
            companions=[who[g]["name"] for g in sorted(company) if g in who],
            story=dict(title=told["title"], words=told["words"], version=told["version"],
                       written_at=told["written_at"]) if told else None))
    return out


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("what", choices=("legs", "write", "publish"))
    ap.add_argument("--guid", type=int, required=True)
    ap.add_argument("--start", default="", help="the journey's first moment, as the legs list gives it")
    ap.add_argument("--era", default=site.get("ERA", "classic"))
    args = ap.parse_args()

    if args.what == "legs":
        for leg in legs_doc(args.guid):
            when = dt.datetime.fromtimestamp(leg["start"]).strftime("%Y-%m-%d %H:%M:%S")
            mins = (leg["end"] - leg["start"]) / 60
            told = f"  [{leg['story']['words']} words]" if leg["story"] else ""
            log(f"{when}  {mins:7.1f} min  {leg['deeds']:3} deeds  {leg['ended']:8}  "
                f"{leg['place']}{told}")
        return 0

    if args.what == "publish":
        log(f"published {publish(args.guid)} stories")
        return 0

    if not args.start:
        ap.error("write needs --start; run `legs` first")
    when = dt.datetime.strptime(args.start, "%Y-%m-%d %H:%M:%S").timestamp()
    out = write_story(args.guid, when, args.era, step=lambda said: log(said))
    log(f"\n{out['title']}\n")
    print(out["body"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
