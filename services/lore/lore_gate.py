#!/usr/bin/env python3
"""lore_gate.py - the player writes their own lore; this decides whether the world can hold it.

custom wow plans/28-PLAN-lore-in-the-browser.md

A player writes a backstory, a bond or a sheet in the browser. This shapes it into era-legal
in-world prose and answers with a verdict. It REFUSES what ripples outward - a later age, a place
that is not that people's, game vocabulary, names the player never wrote - because a main's sheet
is what every alt's bond is written from, and a bond feeds a backstory, traits, prompts, regard and
the chronicle. It only ADVISES on length, voice and repetition, where the player's own wording may
stand (plan 28 §4).

Every refusal says why in the player's own language. That is the point of the thing: it should
teach the fiction, not merely refuse.

Endpoints (JSON; CORS for the dashboard's origin only - mod-dashboard never answers a preflight
and caps bodies at 1024 bytes, which is why this is a separate door):

  GET  /rules?era=classic    the era's setting and the rules in plain words
  GET  /characters[?full=1]  the caller's own characters and what each already has; full=1 adds the prose
  GET  /claim                characters whose account has named no main yet (plan 21 P8)
  GET  /alt-options?character=  the ties this alt may have to its main, and the turns of speech
  GET  /job?id=              how a long piece of writing is coming along
  POST /main-set             name an account's main, which is what turns its others into alts
  POST /alt-write            write a tie and a story for an alt. STORES NOTHING. Returns a job
  POST /draft                shape and check the text. STORES NOTHING. Returns prose + reasons
  POST /alt-keep             store what /alt-write returned, then traits, regard, project, publish
  POST /commit               store an accepted draft, then republish lore-edit.json

Nothing here touches a game object: it runs outside the worldserver like regard and the chronicler,
and writes only the lore tables. Lore reaches prompts after `gen_backstories.py project` and a
worldserver restart.

Stdlib only, like the router. Run: python3 lore_gate.py [--port 8788]
Kill switch: `systemctl --user stop wow-lore-gate`, or `touch /opt/wow/lore/PAUSE`.
"""
import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# The unit runs /opt/wow/lore/lore_gate.py, and /opt/wow/lore is a symlink into the repo. abspath does not
# follow it, so anything reached by walking up from HERE has to start from the real path or it lands in /opt.
REPO = os.path.dirname(os.path.dirname(os.path.realpath(HERE)))

import era as eras          # noqa: E402
import fleet                # noqa: E402
import gen_backstories as g  # noqa: E402

def _site(key, default):
    """A gate setting: the environment first (systemd passes site.env through), then site/ itself,
    so running the gate by hand answers the same way the unit does (plan 23 W15)."""
    return os.environ.get(key) or g.site.get(key, default)


PAUSE_FILE = os.path.join(HERE, "PAUSE")
EDIT_JSON = os.path.join(g.site.get("DATA_DIR", "/opt/wow/server/data"),
                         "dashboard-data", "lore-edit.json")
DEFAULT_LANE = "evo-quality"

# One draft in flight per character, and a floor between calls. The core's own addon flood throttle
# is dead code (plan 28 §1), and nothing else upstream stops a keypress driving inference.
MIN_SECONDS_BETWEEN = 5
_busy = set()
_last_call = {}
_gate_lock = threading.Lock()


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# turning a check into something a person can act on (plan 28 §4)

# Each entry: a matcher against the checker's own message, and what the player is told. The
# pipeline's messages are already the right shape; they only need rewording for a human.
def humanise(message, era_name):
    m = str(message)
    hit = re.match(r"out-of-world term: '(.+)'", m)
    if hit:
        return (f"“{hit.group(1)}” belongs to a later age than this one. "
                f"Nothing past the Third War has happened yet.", True)
    hit = re.match(r"invented names: (.+)", m)
    if hit:
        # Advice, not a refusal. The guard compares the shaped prose against the player's source, and a short
        # source makes almost any shaping look like an invention -- it fired four times on one draft and
        # buried the era break that actually mattered. It also splits multi-word names, so it reports
        # fragments like "Blasted" and "Lands". Worth showing; not worth refusing over.
        return (f"The shaping brought in names you did not write: {hit.group(1)}. "
                f"Anyone bound to this character inherits them, so read them before keeping this.", False)
    hit = re.match(r"judge: (.+)", m)
    if hit:
        return (f"A reader of this age caught something that does not belong: {hit.group(1)}.", True)
    hit = re.match(r"dropped: (.+)", m)
    if hit:
        return (f"Something you wrote went missing: {hit.group(1)}. Your own words should survive "
                f"the shaping, so this was not kept.", True)
    hit = re.match(r"(\d+) words", m)
    if hit:
        return (f"It came to {hit.group(1)} words, which is outside what this kind of text holds. "
                f"You can keep your wording, or let it be trimmed.", False)
    if "second person" in m or "first person" in m or "not second" in m:
        return ("The voice slipped. A character's own story is told as “you”.", False)
    if "carries a figure" in m:
        return ("It carries a number. Nobody in the world counts in figures - say it in words.", True)
    if "overused name" in m:
        return (f"That name is worn out across the world's stories ({m}). Give them one of their own.", False)
    if "does not name" in m:
        return (f"{m}. The text has to say who it is about.", False)
    if "reuses another bond" in m:
        return ("This repeats another character's words almost exactly. Each tie should sound "
                "like nobody else's.", False)
    if "never says it is" in m:
        return (f"{m}. The tie has to actually say what it is.", False)
    return (m, False)


# ---------------------------------------------------------------------------
# the characters a player may write for

def allowed_accounts():
    """Accounts this gate may write for. Naming a main is how an account opts in (plan 21 §1).

    Without this the gate would hand whoever holds the token every real player's characters: the first
    run returned ten belonging to other people. Plan 28 §8 - a commit must never reach another
    player's character.
    """
    if ACCOUNTS:
        return ACCOUNTS
    return [int(r[0]) for r in g.sql("SELECT account_id FROM player_main")]


def characters(account=None):
    """The characters this gate may write for, with what each already has. Never a random bot."""
    accounts = [int(account)] if account else allowed_accounts()
    if not accounts:
        return []
    where = "pk.account IN (" + ",".join(str(a) for a in accounts) + ")"
    rows = g.sql(
        "SELECT pk.guid, c.name, pk.kind, pk.account, c.race, c.class, c.level, "
        "IFNULL(la.kind, ''), IF(lm.guid IS NULL, 0, 1), IF(lc.guid IS NULL, 0, 1), "
        "IF(lc.personality IS NULL, 0, 1) "
        "FROM person_kind pk JOIN characters c ON c.guid = pk.guid "
        "LEFT JOIN lore_alt la ON la.guid = pk.guid "
        "LEFT JOIN lore_main lm ON lm.guid = pk.guid "
        "LEFT JOIN lore_character lc ON lc.guid = pk.guid "
        f"WHERE {where} AND pk.kind <> 'bot' ORDER BY pk.kind DESC, c.name")
    out = []
    for r in rows:
        out.append(dict(guid=int(r[0]), name=r[1], kind=r[2], account=int(r[3]),
                        race=g.RACES.get(int(r[4]), "unknown"), cls=g.CLASSES.get(int(r[5]), "adventurer"),
                        level=int(r[6]), bond_kind=r[7] or None,
                        has_sheet=bool(int(r[8])), has_story=bool(int(r[9])), has_traits=bool(int(r[10]))))
    return out


def character_by_name(name):
    for c in characters():
        if c["name"].lower() == name.lower():
            return c
    return None


# ---------------------------------------------------------------------------
# the gate itself

def shape(char, what, text, era_name, keep):
    """Run the player's words through the pipeline. Returns (prose, refusals, advice).

    Stores nothing. `what` is 'sheet' (a real player's own character) or 'backstory' (an alt).
    """
    e = eras.get(era_name)
    refusals, advice = [], []
    prose = None

    # The player's OWN words come first, before any model call. Two reasons, both learned the hard way on
    # 2026-09-16: a source naming the Wrathgate was refused four times over for names the *model* had added,
    # so the player was told about "Lira" and never about their own era break; and four attempts were spent
    # on a source that could never pass. What the player wrote is what ripples, so it is what is checked.
    bad = e["meta"].search(text)
    if bad:
        said, _ = humanise(f"out-of-world term: '{bad.group(0)}'", era_name)
        return None, [said], []

    lane = fleet.prefer_lanes(DEFAULT_LANE)[0]
    judge = fleet.Judge()

    for attempt in range(1, fleet.MAX_ATTEMPTS + 1):
        try:
            if what == "sheet":
                raw = g.build_main_sheet(lane, dict(name=char["name"], race=_race_id(char),
                                                    cls=_class_id(char), gender=0, level=char["level"]), e, text)
                prose = g.check_main_sheet(raw, char["name"], keep, text, char["race"])
            else:
                bond = g.sql(f"SELECT bond FROM lore_alt WHERE guid = {char['guid']}")
                if not bond:
                    return None, ["This character has no tie to your main yet, so there is nothing to "
                                  "build a story around. Write the tie first."], []
                main = g.main_of(char["guid"])
                c = dict(guid=char["guid"], name=char["name"], race=_race_id(char), cls=_class_id(char),
                         gender=0, level=char["level"], rname=None)
                temper = g.temperaments()[0]
                prompt = g.char_prompt(c, temper, None, e, bond=bond[0][0], main_name=main["name"],
                                       concept=text, main_sheet=main["sheet"], keep=keep)
                prose = g.check_backstory(lane.chat([{"role": "user", "content": prompt}], 600),
                                          g.ALT_STORY_WORDS)
            g.vet(prose, e, judge, attempt, f"gate {char['guid']} {char['name']}")
            break
        except ValueError as err:
            said, hard = humanise(err, era_name)
            (refusals if hard else advice).append(said)
            prose = None
    return prose, _unique(refusals), _unique(advice)


def _unique(items):
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _race_id(char):
    for rid, name in g.RACES.items():
        if name == char["race"]:
            return rid
    return 1


def _class_id(char):
    for cid, name in g.CLASSES.items():
        if name == char["cls"]:
            return cid
    return 1


def commit(char, what, prose, source, era_name):
    lane_model = "player+gate"
    digest = hashlib.sha1(prose.encode("utf-8")).hexdigest()
    if what == "sheet":
        g.sql(f"INSERT INTO lore_main (guid, sheet, source, sheet_hash, model, era) VALUES "
              f"({char['guid']}, {g.q(prose)}, {g.q(source)}, '{digest}', {g.q(lane_model)}, "
              f"{g.q(era_name)}) ON DUPLICATE KEY UPDATE sheet = VALUES(sheet), source = VALUES(source), "
              f"sheet_hash = VALUES(sheet_hash), model = VALUES(model), era = VALUES(era)", fetch=False)
        stale = g.sql(f"SELECT c.name FROM lore_alt la JOIN characters c ON c.guid = la.guid "
                      f"WHERE la.main_guid = {char['guid']} AND (la.sheet_hash IS NULL OR la.sheet_hash <> '{digest}')")
        return [r[0] for r in stale]
    temper = g.temperaments()[0]
    g.sql("INSERT INTO lore_character (guid, guildid, temperament, backstory, source, model, era) VALUES "
          f"({char['guid']}, NULL, {g.q(temper[0])}, {g.q(prose)}, {g.q(source)}, {g.q(lane_model)}, "
          f"{g.q(era_name)}) ON DUPLICATE KEY UPDATE backstory = VALUES(backstory), "
          f"source = VALUES(source), model = VALUES(model), era = VALUES(era)", fetch=False)
    return []


def full_doc():
    """Who the player's characters are, and every text that actually reaches their prompts."""
    doc = dict(generated=int(time.time()), characters=characters())
    for c in doc["characters"]:
        rows = g.sql(f"SELECT {g._flat('lm.sheet')}, {g._flat('lm.source')} FROM lore_main lm "
                     f"WHERE lm.guid = {c['guid']}")
        if rows:
            c["sheet"], c["sheet_source"] = rows[0][0], rows[0][1]
        rows = g.sql(f"SELECT {g._flat('la.bond')}, la.kind FROM lore_alt la WHERE la.guid = {c['guid']}")
        if rows:
            c["bond"], c["bond_kind"] = rows[0][0], rows[0][1]
        # Everything that actually reaches a prompt, so the page can show what stands now rather than only
        # whether something exists. personality + gist + motivation_short are the core tier projected into
        # every prompt; the full backstory and motivation are the tier used when someone speaks to them.
        rows = g.sql(f"SELECT {g._flat('lc.backstory')}, {g._flat('lc.source')}, "
                     f"{g._flat('lc.motivation_short')}, {g._flat('lc.personality')}, "
                     f"{g._flat('lc.gist')}, {g._flat('lc.motivation')}, IFNULL(lc.temperament, '') "
                     f"FROM lore_character lc WHERE lc.guid = {c['guid']}")
        if rows:
            r = rows[0]
            c["backstory"], c["story_source"], c["motivation"] = r[0], r[1], r[2]
            c["personality"], c["gist"], c["motivation_full"], c["temperament"] = r[3], r[4], r[5], r[6]
    return doc


def publish():
    """The page's read file. `generated` matters: api.js only redraws when the stamp changes."""
    doc = full_doc()
    os.makedirs(os.path.dirname(EDIT_JSON), exist_ok=True)
    with open(EDIT_JSON + ".tmp", "w") as f:
        json.dump(doc, f, separators=(",", ":"))
    os.replace(EDIT_JSON + ".tmp", EDIT_JSON)
    return len(doc["characters"])


# ---------------------------------------------------------------------------
# bringing a new alt into the world (plan 21 §3-§4, driven from the browser)
#
# The CLI `alts` command does bond -> story -> traits -> regard -> project in one pass, storing as it goes.
# A player watching a page needs the opposite shape: write it, READ it, and only then keep it. So the two
# halves are two jobs. Nothing here reimplements the pipeline - the prompts, the checks and the judge are
# gen_backstories', and only the two INSERTs are copied, because the bond's is inline in `alts` and there
# is no function to call.

ALT_ATTEMPTS = 4
JOB_KEPT_SECONDS = 1800
LEVEL_GAP = 5           # further behind than this, and a story written now is a story about someone else

_jobs = {}
_jobs_lock = threading.Lock()


def _new_job(char, phase):
    job = dict(id=f"{phase}-{char['guid']}-{int(time.time() * 1000)}", character=char["name"],
               guid=char["guid"], phase=phase, state="running", steps=[], message="",
               refusals=[], notes=[], started=time.time())
    with _jobs_lock:
        _jobs[job["id"]] = job
        for jid, old in list(_jobs.items()):    # a page that was closed must not leak its job forever
            if old["state"] != "running" and time.time() - old.get("finished", old["started"]) > JOB_KEPT_SECONDS:
                _jobs.pop(jid, None)
    return job


def _step(job, said):
    job["steps"].append(said)
    log(f"[{job['id']}] {said}")


def _finish(job, state, message):
    job["state"], job["message"], job["finished"] = state, message, time.time()
    log(f"[{job['id']}] {state}: {message}")


def _start(job, fn):
    """Run a job off the request thread, and free the character whatever happens."""
    def body():
        try:
            fn(job)
        except Exception as err:                # noqa: BLE001 - one bad job must never shut the gate
            _finish(job, "failed", f"It broke off: {err}")
        finally:
            with _gate_lock:
                _busy.discard(job["guid"])
    threading.Thread(target=body, daemon=True, name=job["id"]).start()


def _row(char):
    """The character as gen_backstories wants it: raw ids, straight from the world."""
    r = g.sql(f"SELECT c.race, c.class, c.gender, c.level FROM characters c WHERE c.guid = {char['guid']}")
    if not r:
        return None
    return dict(guid=char["guid"], name=char["name"], race=int(r[0][0]), cls=int(r[0][1]),
                gender=int(r[0][2]), level=int(r[0][3]), rank=None, rname=None)


def _concept_file(name):
    """Words a player left on disk for this character, if they ever did (the CLI's own habit)."""
    path = os.path.join(g.ALTS_DIR, name.lower() + ".txt")
    try:
        return open(path, encoding="utf-8").read().strip() if os.path.exists(path) else ""
    except OSError:
        return ""


def _suggest(c, m, e, allowed):
    """A whole alt, drawn: a tie, a turn of speech, and words to start from (plan 43 R3/R4).

    The words are the part that needed deciding. An empty concept is honest -- both prompts simply drop their
    block without one -- but it reads generic, so this hands over a seed shaped like the sentences a player
    would have written, for them to overwrite. It is a suggestion and stores nothing: the gate draws the tie
    and the temperament itself when those fields arrive blank, and this only puts the same draw on screen
    first, where it can be argued with.
    """
    rng = random.Random()
    kind = rng.choices(allowed, weights=[g.BOND_KINDS[k]["weight"] for k in allowed])[0] if allowed else ""
    tempers = g.temperaments()
    temper = rng.choice(tempers)[0] if tempers else ""
    race = g.RACES.get(c["race"], "").lower()
    cls = g.CLASSES.get(c["cls"], "adventurer").lower()
    words = f"{c['name']} is a {race} {cls}, {eras.standing(c['level'], e)}.".replace("  ", " ")
    if kind:
        words += f" Between them and {m['name']}: {g.BOND_KINDS[kind]['gloss']}."
    if temper:
        # Not "like a {x}": the keys are adjectives and nouns mixed, so the article breaks on half of them
        # ("like a grieving", "like a cheerful"). This is the phrasing the panel already uses as its heading.
        words += f" Their turn of speech is {temper.lower().replace('_', ' ')}."
    return dict(kind=kind, temperament=temper, concept=words)


def alt_options(char):
    """What this alt may be to its main, and what it already carries."""
    e = eras.get(ERA)
    c = _row(char)
    m = g.main_of(char["guid"])
    if not c or not m:
        return None
    rows = g.sql(f"SELECT level FROM characters WHERE guid = {m['guid']}")
    main_level = int(rows[0][0]) if rows else 0
    rows = g.sql(f"SELECT {g._flat('source')} FROM lore_alt WHERE guid = {c['guid']}")
    concept = (rows[0][0] if rows and rows[0][0] else "") or _concept_file(char["name"])
    behind = main_level - c["level"]
    allowed = g.allowed_kinds(c, m)
    return dict(
        character=char["name"], main=m["name"], level=c["level"], main_level=main_level,
        kinds=[dict(kind=k, gloss=g.BOND_KINDS[k]["gloss"]) for k in allowed],
        suggest=_suggest(c, m, e, allowed),
        temperaments=[t[0] for t in g.temperaments()],
        has_bond=bool(char["bond_kind"]), bond_kind=char["bond_kind"],
        has_story=char["has_story"], has_traits=char["has_traits"],
        has_sheet=bool(m["sheet"]), concept=concept, era=e["name"],
        # A story is written for where they stand now (eras.standing). Write it at level one and it is a
        # story about someone who has seen nothing - not who they are an hour after catching up.
        catchup=(f"{char['name']} stands far behind {m['name']}. Catch them up in the world first — "
                 f"CleanBot's Catch Up button, or `.playerbots bot catchup {char['name']}` — or their "
                 f"story will be written for someone who has never left the road.")
        if behind >= LEVEL_GAP else None)


def claimable():
    """Characters whose own account has named no main. Naming one is what makes the rest of them alts.

    This used to answer only on a realm where *nobody* had named a main (plan 43 H1). The guard was aimed
    at the right risk -- holding the token does not prove you own an account (plan 28 §8) -- but it fired
    realm-wide, so the moment the first person named a main the question could never be asked again by
    anyone. A second household could not be created from the browser at all. Scoping it per account keeps
    the protection that matters (an account that has already spoken for itself is never offered again) and
    drops the part that made the feature single-use. Under the trusted-realm decision (43 §2) the token's
    holder is assumed to be someone the operator would hand the GM console to.

    Service accounts are skipped. The merchants that run the auction house are, structurally, an account
    with characters and no main -- so without this they appear under "Name your main", and claiming one
    would turn the other nine into alts, which after the person_kind sweep puts the auction house inside
    company recruiting. Override with LORE_GATE_SKIP_ACCOUNTS if a realm names its merchants differently.
    """
    raw = os.environ.get("LORE_GATE_SKIP_ACCOUNTS") or g.site.get("LORE_GATE_SKIP_ACCOUNTS", "MERCHANTS")
    skip = [s.strip() for s in raw.split(",") if s.strip()]
    where = f"AND c.account IN ({','.join(str(a) for a in ACCOUNTS)})" if ACCOUNTS else ""
    if skip:
        where += " AND a.username NOT IN (" + ",".join(g.q(s) for s in skip) + ")"
    rows = g.sql("SELECT c.guid, c.name, c.account, c.race, c.class, c.level FROM characters c "
                 f"JOIN {g.AUTH_DB}.account a ON a.id = c.account "
                 "LEFT JOIN player_main pm ON pm.account_id = c.account "
                 f"WHERE a.username NOT LIKE 'RNDBOT%' AND pm.account_id IS NULL {where} "
                 "ORDER BY c.level DESC, c.name")
    return [dict(guid=int(r[0]), name=r[1], account=int(r[2]), race=g.RACES.get(int(r[3]), "unknown"),
                 cls=g.CLASSES.get(int(r[4]), "adventurer"), level=int(r[5])) for r in rows]


def name_main(guid, account):
    g.sql(f"INSERT INTO player_main (account_id, main_guid) VALUES ({int(account)}, {int(guid)}) "
          f"ON DUPLICATE KEY UPDATE main_guid = VALUES(main_guid), set_at = CURRENT_TIMESTAMP", fetch=False)


def _lanes():
    lane = fleet.prefer_lanes(DEFAULT_LANE)[0]
    # Traits come back as JSON, and the lanes say which of them can be trusted with it.
    traits = next((l for l in fleet.prefer_lanes(None) if "traits" in l.kinds), lane)
    return lane, traits, fleet.Judge()


def _note_attempt(job, err, what, attempt):
    """Why one attempt was thrown away. Only a hard fault is a refusal; the rest is the band being missed.

    humanise's wording is written for the draft box, where a player may keep their own phrasing against the
    advice. Here nothing of theirs is being trimmed - the model overshot and was asked again - so offering to
    "keep your wording" would name a choice that does not exist, over words the player never wrote.
    """
    hit = re.match(r"(\d+) words", str(err))
    if hit:
        lo, hi = g.BOND_WORDS if what == "tie" else g.ALT_STORY_WORDS
        job["notes"].append(f"One attempt came to {hit.group(1)} words, and a {what} must be {lo} to {hi}, "
                            f"so it was written again.")
    else:
        said, hard = humanise(err, ERA)
        (job["refusals"] if hard else job["notes"]).append(said)
    _step(job, f"That {what} did not hold ({attempt}).")


def write_alt(job, char, kind, temperament, concept, keep, keep_story):
    """The tie, then the story built around it. Stores nothing at all."""
    e = eras.get(ERA)
    c, m = _row(char), g.main_of(char["guid"])
    lane, _, judge = _lanes()
    rng = random.Random()

    other_rows = g.sql("SELECT c2.name, la.kind, la.bond FROM lore_alt la "
                       "JOIN characters c2 ON c2.guid = la.guid "
                       f"WHERE la.main_guid = {m['guid']} AND la.guid <> {c['guid']}")
    others, other_bonds = [(r[0], r[1]) for r in other_rows], [r[2] for r in other_rows]

    allowed = g.allowed_kinds(c, m)
    if kind and kind not in allowed:
        return _finish(job, "failed", f"{char['name']} cannot be {kind} to {m['name']}. "
                                      f"What they could be: {', '.join(allowed)}.")
    if not kind:
        kind = rng.choices(allowed, weights=[g.BOND_KINDS[k]["weight"] for k in allowed])[0]
        job["drawn_kind"] = True
    job["kind"], job["pinned"] = kind, 0 if job.get("drawn_kind") else 1

    _step(job, f"Asking what stands between {char['name']} and {m['name']} — {kind}.")
    for attempt in range(1, ALT_ATTEMPTS + 1):
        try:
            bond = g.build_bond(lane, c, m, kind, concept, others, e, keep)
            g.check_bond_extra(bond, kind, keep, other_bonds)
            g.vet(bond, e, judge, attempt, f"gate bond {c['guid']} {c['name']}")
            job["bond"] = bond
            break
        except ValueError as err:
            _note_attempt(job, err, "tie", attempt)
    if not job.get("bond"):
        job["refusals"], job["notes"] = _unique(job["refusals"]), _unique(job["notes"])
        return _finish(job, "failed", "No tie came back that the world would hold. "
                                      "Try saying more about what is between them.")

    tempers = g.temperaments()
    temper = next((t for t in tempers if t[0].upper() == (temperament or "").upper()), None)
    if temper is None:
        # A drawn temperament contradicts the player's own words often enough that the CLI carries a
        # comment about it. When they choose, it is pinned; when they do not, it is at least said out loud.
        temper = rng.choice(tempers)
        job["drawn_temperament"] = True
    job["temperament"] = temper[0]

    _step(job, f"Writing {char['name']}'s own story, in the voice of a "
               f"{temper[0].lower().replace('_', ' ')}.")
    for attempt in range(1, ALT_ATTEMPTS + 1):
        try:
            prompt = g.char_prompt(c, temper, None, e, bond=job["bond"], main_name=m["name"],
                                   concept=concept, main_sheet=m["sheet"], keep=keep_story)
            story = g.check_backstory(lane.chat([{"role": "user", "content": prompt}], 600),
                                      g.ALT_STORY_WORDS)
            g.vet(story, e, judge, attempt, f"gate story {c['guid']} {c['name']}")
            job["story"] = story
            break
        except ValueError as err:
            _note_attempt(job, err, "story", attempt)
    job["refusals"], job["notes"] = _unique(job["refusals"]), _unique(job["notes"])
    if not job.get("story"):
        return _finish(job, "failed", "The tie stands, but no story came back that the world would hold.")
    _finish(job, "done", "Read them both. Nothing is kept until you say so.")


def keep_alt(job, char, src, concept):
    """Store what was read and approved, then everything that follows from it."""
    e = eras.get(ERA)
    c, m = _row(char), g.main_of(char["guid"])
    _, trait_lane, judge = _lanes()
    bond, story, kind, temper = src["bond"], src["story"], src["kind"], src["temperament"]

    # Never store unchecked prose, even our own: the hard check runs again on exactly what goes in.
    for text in (bond, story):
        bad = e["meta"].search(text)
        if bad:
            said, _ = humanise(f"out-of-world term: '{bad.group(0)}'", ERA)
            return _finish(job, "failed", said)

    _step(job, f"Binding {char['name']} to {m['name']} as {kind}.")
    g.sql(f"INSERT INTO lore_alt (guid, main_guid, kind, pinned, bond, sheet_hash, source, model, era) "
          f"VALUES ({c['guid']}, {m['guid']}, {g.q(kind)}, {int(src.get('pinned', 1))}, {g.q(bond)}, "
          f"{g.q(m['sheet_hash'] or '')}, {g.q(concept)}, {g.q('player+gate')}, {g.q(e['name'])}) "
          f"ON DUPLICATE KEY UPDATE main_guid = VALUES(main_guid), kind = VALUES(kind), "
          f"pinned = VALUES(pinned), bond = VALUES(bond), sheet_hash = VALUES(sheet_hash), "
          f"source = VALUES(source), model = VALUES(model), era = VALUES(era)", fetch=False)

    _step(job, "Setting down their story.")
    g.sql("INSERT INTO lore_character (guid, guildid, temperament, backstory, source, model, era) VALUES "
          f"({c['guid']}, NULL, {g.q(temper)}, {g.q(story)}, {g.q(concept)}, {g.q('player+gate')}, "
          f"{g.q(e['name'])}) ON DUPLICATE KEY UPDATE temperament = VALUES(temperament), "
          f"backstory = VALUES(backstory), source = VALUES(source), model = VALUES(model), "
          f"era = VALUES(era)", fetch=False)

    # Traits UPDATE the row the story just made. Without it they write nothing and report success.
    _step(job, "Giving them a way of being in a room, and something to live for.")
    rows = g.trait_rows(e, where=f"l.guid = {c['guid']}")
    if not rows:
        _step(job, "Their story did not take, so there was nothing to give traits to.")
    else:
        rng = random.Random()
        for attempt in range(1, ALT_ATTEMPTS + 1):
            try:
                g.gen_traits(trait_lane, attempt, e, judge, rows[0], rng)
                break
            except ValueError as err:
                _step(job, f"That did not hold ({attempt}): {err}")

    _step(job, "Seeding what they already feel about their own.")
    seeded = g.seed_bond_regard(e, [c["guid"]])
    _step(job, f"{seeded} feelings seeded." if seeded else "No new feelings to seed.")

    _step(job, "Projecting all of it into what they say.")
    g.project(None)
    publish()

    told = reload_prompts(job)
    _finish(job, "done", f"{char['name']} is one of yours now."
            + ("" if told else " Run `.ollama reload` on the world's console for them to know it."))


def reload_prompts(job):
    """Tell the running world to re-read the templates. Best effort: the gate holds no console of its own."""
    script = os.environ.get("LORE_GATE_RELOAD", os.path.join(REPO, "ops", "scripts", "ra.sh"))
    if not os.path.exists(script):
        return False
    try:
        done = subprocess.run([script, "ollama reload"], capture_output=True, text=True, timeout=90)
        ok = done.returncode == 0 and bool(done.stdout.strip())
    except Exception:                           # noqa: BLE001 - a console that will not answer is not an error
        ok = False
    _step(job, "The world has been told; they know it now." if ok
          else "The world's console did not answer, so they do not know it yet.")
    return ok


# ---------------------------------------------------------------------------
# the door

class Handler(BaseHTTPRequestHandler):
    server_version = "loregate/1.0"

    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")

    def _cors(self):
        # The dashboard can be reached by more than one name - the tailnet address from the game PC, and
        # localhost from the box itself - and the page dials the gate at whatever host it was served from.
        # One fixed origin refuses the others, and a browser reports that as a bare network error.
        origin = self.headers.get("Origin", "")
        allowed = origin if origin and (ORIGINS == ["*"] or origin in ORIGINS) else (ORIGINS[0] if ORIGINS else "*")
        self.send_header("Access-Control-Allow-Origin", allowed)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Lore-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, code, body):
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _authed(self):
        if not TOKEN:
            self._send(503, {"ok": False, "message": "The gate has no token set, so it stays shut."})
            return False
        given = self.headers.get("X-Lore-Token", "")
        if len(given) != len(TOKEN) or not _constant_eq(given, TOKEN):
            self._send(401, {"ok": False, "message": "That token is not this gate's."})
            return False
        if os.path.exists(PAUSE_FILE):
            self._send(503, {"ok": False, "message": "The gate is paused."})
            return False
        return True

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if not self._authed():
            return
        if self.path.startswith("/rules"):
            e = eras.get(ERA)
            return self._send(200, {"ok": True, "era": ERA, "setting": e["setting"],
                                    "rules": RULES_IN_WORDS})
        if self.path.startswith("/characters"):
            # full=1 is the whole read document, live. The page polls a file that is only rewritten on a
            # commit, so a character made since the last one is invisible there until something republishes.
            if (self._query().get("full") or [""])[0]:
                return self._send(200, dict(ok=True, **full_doc()))
            return self._send(200, {"ok": True, "characters": characters()})
        if self.path.startswith("/claim"):
            return self._send(200, {"ok": True, "claimable": claimable()})
        if self.path.startswith("/alt-options"):
            name = (self._query().get("character") or [""])[0]
            char = character_by_name(name)
            if not char:
                return self._send(404, {"ok": False, "message": f"There is nobody called {name}."})
            options = alt_options(char)
            if not options:
                return self._send(400, {"ok": False,
                                        "message": f"{char['name']} has no main to be bound to."})
            return self._send(200, {"ok": True, "options": options})
        if self.path.startswith("/job"):
            with _jobs_lock:
                job = _jobs.get((self._query().get("id") or [""])[0])
            if not job:
                return self._send(404, {"ok": False, "message": "That piece of writing is forgotten."})
            return self._send(200, {"ok": True, "job": job})
        self._send(404, {"ok": False, "message": "No such door."})

    def do_POST(self):
        if not self._authed():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, {"ok": False, "message": "That was not readable."})

        # The alt flow runs long and answers with a job, so it takes the door before the draft's own
        # validation, which is written for a character that already has a tie.
        if self.path.startswith("/main-set"):
            return self._main_set(body)
        if self.path.startswith("/alt-write"):
            return self._alt_job(body, keep=False)
        if self.path.startswith("/alt-keep"):
            return self._alt_job(body, keep=True)

        name = str(body.get("character", "")).strip()
        what = str(body.get("what", "")).strip()
        text = str(body.get("text", "")).strip()
        keep = [k.strip() for k in str(body.get("keep", "")).split(",") if k.strip()]
        char = character_by_name(name)
        if not char:
            return self._send(404, {"ok": False, "message": f"There is nobody called {name} to write for."})
        if what not in ("sheet", "backstory"):
            return self._send(400, {"ok": False, "message": "Say whether this is a sheet or a backstory."})
        if what == "sheet" and char["kind"] != "main":
            return self._send(400, {"ok": False,
                                    "message": f"{char['name']} is not your main, so they have no sheet."})
        if not text:
            return self._send(400, {"ok": False, "message": "There is nothing written yet."})

        with _gate_lock:
            now = time.time()
            if char["guid"] in _busy:
                return self._send(429, {"ok": False, "message": f"{char['name']} is already being written."})
            if now - _last_call.get(char["guid"], 0) < MIN_SECONDS_BETWEEN:
                return self._send(429, {"ok": False, "message": "Give it a moment before trying again."})
            _busy.add(char["guid"])
            _last_call[char["guid"]] = now
        try:
            if self.path.startswith("/draft"):
                prose, refusals, advice = shape(char, what, text, ERA, keep)
                return self._send(200, {"ok": bool(prose), "character": char["name"], "what": what,
                                        "prose": prose, "yours": text,
                                        "refusals": refusals, "advice": advice})
            if self.path.startswith("/commit"):
                prose = str(body.get("prose", "")).strip()
                if not prose:
                    return self._send(400, {"ok": False, "message": "There is no accepted draft to keep."})
                # Never store unchecked prose: a commit re-runs the hard checks on exactly what is sent.
                e = eras.get(ERA)
                bad = e["meta"].search(prose)
                if bad:
                    said, _ = humanise(f"out-of-world term: '{bad.group(0)}'", ERA)
                    return self._send(400, {"ok": False, "message": said})
                stale = commit(char, what, prose, text, ERA)
                n = publish()
                return self._send(200, {"ok": True, "message": f"Kept for {char['name']}.",
                                        "stale_bonds": stale, "characters": n,
                                        "note": "It reaches what they say after `project` and a restart."})
            self._send(404, {"ok": False, "message": "No such door."})
        finally:
            with _gate_lock:
                _busy.discard(char["guid"])

    # -- naming a main, and bringing an alt in ------------------------------

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def _main_set(self, body):
        name = str(body.get("character", "")).strip()
        who = next((c for c in claimable() if c["name"].lower() == name.lower()), None)
        if not who:
            return self._send(404, {"ok": False, "message":
                                    f"There is nobody called {name} whose account is still unspoken for."})
        name_main(who["guid"], who["account"])
        log(f"main named: account {who['account']} -> {who['name']} ({who['guid']})")
        return self._send(200, {"ok": True, "characters": publish(), "message":
                                f"{who['name']} is your own. Every other character on that account is now "
                                f"theirs to be bound to."})

    def _alt_job(self, body, keep):
        name = str(body.get("character", "")).strip()
        char = character_by_name(name)
        if not char:
            return self._send(404, {"ok": False, "message": f"There is nobody called {name} to write for."})
        if char["kind"] != "alt":
            return self._send(400, {"ok": False, "message":
                                    f"{char['name']} is not an alt, so there is nobody to bind them to."})
        if not g.main_of(char["guid"]):
            return self._send(400, {"ok": False, "message": "This account has named no main yet."})
        concept = str(body.get("concept", "")).strip()

        with _gate_lock:
            if char["guid"] in _busy:
                return self._send(429, {"ok": False,
                                        "message": f"{char['name']} is already being written."})
            _busy.add(char["guid"])
            _last_call[char["guid"]] = time.time()

        def refuse(message):
            with _gate_lock:
                _busy.discard(char["guid"])
            return self._send(400, {"ok": False, "message": message})

        if keep:
            # Keep what the gate itself wrote and the player read, never prose posted back to us: the
            # bond and story are taken from the job by id, not from the body.
            with _jobs_lock:
                src = _jobs.get(str(body.get("job", "")))
            if not src or src.get("guid") != char["guid"] or src.get("state") != "done" \
                    or not src.get("story") or not src.get("bond"):
                return refuse("There is nothing written to keep. Write it first.")
            job = _new_job(char, "keep")
            _start(job, lambda j: keep_alt(j, char, src, concept or src.get("concept", "")))
        else:
            if not concept:
                return refuse("Say who they are first, in your own words.")
            if char["bond_kind"] and not body.get("rebond"):
                return refuse(f"{char['name']} is already bound to their main as {char['bond_kind']}. "
                              f"Say so plainly if you mean to write it again.")
            job = _new_job(char, "write")
            job["concept"] = concept
            _start(job, lambda j: write_alt(
                j, char, str(body.get("kind", "")).strip(), str(body.get("temperament", "")).strip(),
                concept,
                [k.strip() for k in str(body.get("keep", "")).split(",") if k.strip()],
                [k.strip() for k in str(body.get("keep_story", "")).split(",") if k.strip()]))
        return self._send(200, {"ok": True, "job": job["id"], "state": job["state"]})


def _constant_eq(a, b):
    diff = 0
    for x, y in zip(a, b):
        diff |= ord(x) ^ ord(y)
    return diff == 0


RULES_IN_WORDS = [
    "Write as though it is real life. No games, no levels as numbers, no figures.",
    "Nothing later than this age has happened yet.",
    "Keep to your own people's lands and history.",
    "Invent whoever you like - but give them names of their own, not famous ones.",
    "Your own words are kept. The shaping must not lose what you said.",
]

TOKEN = ""
ORIGINS = ["*"]
ERA = g.site.get("ERA", "classic")   # the world's clock, from site.env (plan 23 W8)
ACCOUNTS = []


def main():
    global TOKEN, ORIGINS, ERA, ACCOUNTS
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(_site("LORE_GATE_PORT", "8788")))
    ap.add_argument("--bind", default=_site("LORE_GATE_BIND", "127.0.0.1"))
    ap.add_argument("--origin", default=_site("LORE_GATE_ORIGIN", "*"),
                    help="the dashboard's origins, comma-separated, e.g. "
                         "http://127.0.0.1:8787,http://localhost:8787")
    ap.add_argument("--era", default=os.environ.get("LORE_GATE_ERA") or g.site.get("ERA", "classic"))
    ap.add_argument("--accounts", default=_site("LORE_GATE_ACCOUNTS", ""),
                    help="comma-separated account ids this gate may write for; "
                         "default every account that has named a main")
    ap.add_argument("--publish-only", action="store_true", help="write lore-edit.json and exit")
    args = ap.parse_args()

    TOKEN = (os.environ.get("LORE_GATE_TOKEN") or g.site.get("LORE_GATE_TOKEN", "")).strip()
    ORIGINS = [o.strip() for o in args.origin.split(",") if o.strip()] or ["*"]
    ERA = args.era
    ACCOUNTS = [int(a) for a in args.accounts.replace(",", " ").split() if a.strip().isdigit()]
    g.ensure_schema()
    log(f"writing for accounts: {ACCOUNTS or allowed_accounts()}")

    if args.publish_only:
        log(f"published {publish()} characters to {EDIT_JSON}")
        return 0
    if not TOKEN:
        log("WARNING: LORE_GATE_TOKEN is not set; every request will be refused.")
    log(f"published {publish()} characters to {EDIT_JSON}")
    log(f"era {ERA}; listening on http://{args.bind}:{args.port}/ ; origins {', '.join(ORIGINS)}")
    ThreadingHTTPServer((args.bind, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
