#!/usr/bin/env python3
"""Bot long-term memory, repaired after the fact (plans/30 §4).

mod-ollama-chat condenses a bot's conversations into things it still knows tomorrow. It had never run: the
deque it reads was trimmed to 5 turns (~300 tokens) while the threshold to condense was 1500, so in seven
days of play not one memory was written by it. Every row in `mod_ollama_chat_memories` came instead from the
held-tongue path, and 41 of 48 said some version of "I kept my doubts to myself".

The module is fixed, but it only ever looks at the live deque, so everything said before the fix would stay
unremembered. `ledger_chat` kept all of it. This reads the conversations back out of the ledger, runs them
through the same condensation prompt the module uses, and writes the memories the bots should have had.

  prune     [--apply]            drop the rows the fixed module would never write
  backfill  [--since D] [--apply]  condense past conversations into memories
  events    [--since T] [--apply]  digest past DEEDS into memories (plan 38)
  show      --bot NAME           what one bot remembers

Both writing commands are dry runs unless given --apply.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

# The sibling services, found from this file instead of through the /opt/wow symlinks (plan 23 W2), so a
# checkout runs wherever it is put.
_SERVICES = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(_SERVICES, "lore"))
import era as eras  # noqa: E402
import fleet  # noqa: E402

# Where this realm lives now comes from site/, with the live server config as the fallback (plan 23 W1).
sys.path.insert(0, _SERVICES)
from common import site  # noqa: E402

MODULE_CONF = site.server_conf(os.path.join("modules", "mod_ollama_chat.conf"))

# A session ends when nobody has spoken for this long; each one is condensed on its own, the way the module
# would have condensed a window as it filled.
SESSION_GAP_MINUTES = 45
MIN_TURNS = 4            # below this there is nothing worth remembering
MAX_PER_BOT = 40         # OllamaChat.Memory.MaxPerBot

# What the module's held-tongue path used to write into the same table, before it was made to keep only what
# mattered. These are memories of having said nothing, and they taught bots to say nothing.
HOLLOW = re.compile(r"^\s*(i (kept|did not|didn't|withheld|said nothing|stayed silent)|"
                    r"\w+ kept (the|my|his|her|their) (thought|question|doubt|silence)|"
                    r"[^.]{0,40}\b(was|were) not mine to (speak|say|tell))", re.I)


def words_of(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def is_echo(thought, said):
    """The same test the module now applies before storing a held tongue (dispatch.cpp IsEcho): asked what
    it swallowed, a model sometimes answers by handing back the line that prompted the question, and that
    sentence then sits in memory until the bot says it to the person who said it first."""
    a, b = words_of(thought), set(words_of(said))
    if len(a) < 4 or not b:
        return False
    return sum(1 for w in a if w in b) * 4 >= len(a) * 3


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


_DB = site.db("characters")


def sql(query, fetch=True):
    host, port, user, pw, db = _DB
    out = subprocess.run(["mysql", "--default-character-set=utf8mb4", "-h", host, "-P", port,
                          "-u", user, db, "--batch", "--raw", "-N"],
                         input=query, env={"MYSQL_PWD": pw, "PATH": "/usr/bin:/bin"},
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return [row.split("\t") for row in out.stdout.splitlines() if row] if fetch else []


ASCII_PUNCT = str.maketrans({"—": " - ", "–": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
                             "…": "...", " ": " "})


def q(s):
    s = str(s).translate(ASCII_PUNCT)
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def condense_prompt():
    """The module's own prompt, read from the live config so the two cannot drift apart."""
    for line in open(MODULE_CONF, encoding="utf-8", errors="replace"):
        if line.startswith("OllamaChat.Memory.CondensePrompt"):
            raw = line.split("=", 1)[1].strip()
            if raw.startswith('"'):
                raw = raw[1:raw.rindex('"')]
            return raw.replace("\\n", "\n").replace('\\"', '"')
    sys.exit("OllamaChat.Memory.CondensePrompt not found in " + MODULE_CONF)


def event_prompt():
    """The module's own deed prompt, read from the live config for the same reason as above."""
    for line in open(MODULE_CONF, encoding="utf-8", errors="replace"):
        if line.startswith("OllamaChat.Memory.EventPrompt"):
            raw = line.split("=", 1)[1].strip()
            if raw.startswith('"'):
                raw = raw[1:raw.rindex('"')]
            raw = raw.replace("\\n", "\n").replace('\\"', '"')
            if raw.strip():
                return raw
    sys.exit("OllamaChat.Memory.EventPrompt is unset in " + MODULE_CONF +
             " -- the module falls back to a compiled default, but this tool will not guess it")


# ---------------------------------------------------------------------------

def prune(args):
    rows = sql("SELECT id, bot_guid, importance, REPLACE(memory_text, '\\t', ' ') FROM mod_ollama_chat_memories "
               "ORDER BY id")
    # Anything a person said in the minutes before the memory was written is a candidate for it to be a
    # parrot of. Cheap: the whole ledger of human speech is a few hundred lines.
    heard = [(float(r[0]), "\t".join(r[1:])) for r in
             sql("SELECT UNIX_TIMESTAMP(ts), REPLACE(text, '\t', ' ') FROM ledger_chat "
                 "WHERE speaker_is_bot = 0 ORDER BY ts")]
    stamps = {int(r[0]): float(r[1]) for r in
              sql("SELECT id, UNIX_TIMESTAMP(created_at) FROM mod_ollama_chat_memories")}

    doomed, echoes = [], []
    for r in rows:
        text = "\t".join(r[3:])
        if HOLLOW.match(text):
            doomed.append(r)
            continue
        at = stamps.get(int(r[0]), 0.0)
        if any(is_echo(text, said) for ts, said in heard if 0 <= at - ts <= 600):
            echoes.append(r)
            doomed.append(r)
    log(f"{len(rows)} memories; {len(doomed) - len(echoes)} say only that the bot said nothing, "
        f"{len(echoes)} are a person's own words handed back")
    for r in echoes:
        log(f"  echo {r[0]:>4}  {' '.join(r[3:])[:90]}")
    for r in doomed[:10]:
        log(f"  {r[0]:>4}  {' '.join(r[3:])[:90]}")
    if len(doomed) > 10:
        log(f"  ... and {len(doomed) - 10} more")
    if not doomed:
        return 0
    if not args.apply:
        log("dry run; pass --apply to delete")
        return 0
    sql(f"DELETE FROM mod_ollama_chat_memories WHERE id IN ({','.join(r[0] for r in doomed)})", fetch=False)
    log(f"deleted {len(doomed)}")
    return 0


def sessions(args):
    """Every conversation a bot took part in, as the transcript it would have seen.

    The module keeps one deque per (bot, player) and renders it "Name: ...\\nYou: ...". A conversation in a
    party is not two people though, and the ledger kept who else was there, so a session here is the whole
    exchange in one zone: the bot's own lines marked "You", everyone else's under their name. That is more
    of the truth than the module's own window held, not less.
    """
    since = f"AND c.ts > NOW() - INTERVAL {int(args.since)} DAY" if args.since else ""
    rows = sql("SELECT c.speaker_guid, c.speaker_name, c.speaker_is_bot, c.zone_id, "
               f"UNIX_TIMESTAMP(c.ts), REPLACE(REPLACE(c.text, '\\t', ' '), '\\n', ' ') "
               f"FROM ledger_chat c WHERE c.text <> '' {since} ORDER BY c.ts, c.id")
    lines = [dict(guid=int(r[0]), name=r[1], bot=r[2] == "1", zone=int(r[3]), ts=float(r[4]),
                  text="\t".join(r[5:])) for r in rows]

    # Which bots spoke at all, and when. A bot only remembers conversations it was part of.
    out = []
    speakers = sorted({l["guid"] for l in lines if l["bot"]})
    for guid in speakers:
        mine = [l for l in lines if l["guid"] == guid]
        name = mine[0]["name"]
        blocks, cur = [], []
        last = None
        for l in mine:
            if last is not None and l["ts"] - last > SESSION_GAP_MINUTES * 60:
                blocks.append(cur)
                cur = []
            cur.append(l)
            last = l["ts"]
        if cur:
            blocks.append(cur)

        for block in blocks:
            start, end, zone = block[0]["ts"], block[-1]["ts"], block[0]["zone"]
            window = [l for l in lines
                      if start - 120 <= l["ts"] <= end + 120 and l["zone"] == zone]
            # Nothing to remember from a monologue with no one in it.
            if not any(not l["bot"] for l in window) or len(window) < MIN_TURNS:
                continue
            text = "".join(("You: " if l["guid"] == guid else l["name"] + ": ") + l["text"] + "\n"
                           for l in window)
            out.append(dict(guid=guid, name=name, at=end, turns=len(window), history=text))
    return out


MEMORY_LINE = re.compile(r"^\s*(?:(\d{1,2})\s*\|\s*)?(.+?)\s*$")
ERA = eras.get(site.get("ERA", "classic"))   # site.env, not a second env var of its own (plan 23 W8)


def parse_memories(text):
    found = []
    for raw in text.splitlines():
        raw = raw.strip().lstrip("-*• ").strip()
        if not raw or raw.startswith("#"):
            continue
        m = MEMORY_LINE.match(raw)
        if not m:
            continue
        importance = int(m.group(1)) if m.group(1) else 5
        body = m.group(2).strip().strip('"')
        if len(body.split()) < 3 or len(body) > 400:
            continue
        if HOLLOW.match(body):
            continue          # the very thing this exists to stop writing
        if ERA["meta"].search(body):
            continue          # a later age leaking in: 1 in 331 on the first run said "Howling Fjord"
        found.append((max(1, min(10, importance)), body))
    return found[:6]


def backfill(args):
    convos = sessions(args)
    by_bot = {}
    for c in convos:
        by_bot.setdefault(c["guid"], []).append(c)
    log(f"{len(convos)} conversations across {len(by_bot)} bots "
        f"(sessions split on a {SESSION_GAP_MINUTES} min silence)")

    have = {int(r[0]): int(r[1]) for r in
            sql("SELECT bot_guid, COUNT(*) FROM mod_ollama_chat_memories GROUP BY bot_guid")}
    prompt_template = condense_prompt()

    if args.dry_sample:
        c = max(convos, key=lambda c: c["turns"])
        print(prompt_template.format(bot_name=c["name"], history=c["history"])[:2600])
        return 0
    if not args.apply:
        for guid, cs in sorted(by_bot.items(), key=lambda kv: -sum(c["turns"] for c in kv[1]))[:12]:
            log(f"  {cs[0]['name']:<14} {len(cs)} conversations, {sum(c['turns'] for c in cs)} turns, "
                f"{have.get(guid, 0)} memories now")
        log("dry run; pass --apply to write")
        return 0

    lanes = [l for l in fleet.prefer_lanes(args.lanes, args.slots) if "traits" in l.kinds]
    written = []

    def job(c):
        def run(lane, attempt):
            text = lane.chat([{"role": "user",
                               "content": prompt_template.format(bot_name=c["name"], history=c["history"])}],
                             400, temperature=0.7)
            got = parse_memories(text.translate(ASCII_PUNCT))
            if not got:
                return "nothing worth keeping"
            written.append((c, got))
            return f"{len(got)} from {c['turns']} turns"
        return run

    pool = fleet.Pool(lanes, log=lambda m: log(m) if "FAILED" in m or "pool finished" in m else None)
    for c in convos:
        pool.submit("traits", 1, job(c), f"recall {c['guid']} {c['name']} @{int(c['at'])}")
    failures = pool.run()

    values, kept = [], {}
    for c, got in sorted(written, key=lambda w: w[0]["at"]):
        for importance, body in got:
            n = kept.get(c["guid"], have.get(c["guid"], 0))
            if n >= MAX_PER_BOT:
                continue
            kept[c["guid"]] = n + 1
            values.append(f"({c['guid']}, {q(body[:400])}, {importance}, FROM_UNIXTIME({int(c['at'])}))")
    if values:
        for i in range(0, len(values), 200):
            sql("INSERT INTO mod_ollama_chat_memories (bot_guid, memory_text, importance, created_at) "
                "VALUES " + ",".join(values[i:i + 200]), fetch=False)
    log(f"wrote {len(values)} memories for {len(kept)} bots; {len(failures)} conversations failed")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Event memories (plan 38): what a bot DID, not only what was said to it.
#
# The module now digests deeds into memories as they happen, but only from the night it was built. Every
# dungeon cleared and every death before that is in ledger_event and nowhere else, so a companion who stood
# beside you in the Deadmines has no way to mention it. This reads those deeds back and runs them through
# the module's own EventPrompt, the way backfill does for conversations.
#
# Two things this knows that the live path cannot:
#   * the ledger keeps the creature's `rank`, which DispatchGameEvent throws away (it is handed strings),
#     so a rare spawn can count here where in the module it arrives as an ordinary kill;
#   * group_join / group_leave replay who actually stood with you, where the live path can only ask who
#     happened to be within a hundred yards at the time.

OUTING_GAP_MINUTES = 45      # a stretch of deeds with no longer a lull is one outing
MIN_DEEDS = 3                # two is an incident, not an outing
#
# Raising this from two changed nothing, which is worth recording: the outings that could only be placed in
# "the wilds" are not small, they are in a CITY. `witnessed` counts the whole company's deeds, so a moment
# with three or four people in it clears any sane floor. Zone 1519 and its kind are simply absent from the
# zone list, which holds the levelling zones only. The floor stays at three because it is right anyway; the
# place problem is solved where places are named, not here.
MAX_DEEDS_PER_PROMPT = 40    # a long night must not build a prompt the model cannot hold

NOTABLE = ("death", "quest_complete", "level_up", "loot_item", "kill")


def deed_of(kind, detail):
    """The module's own phrasing (events.cpp MemoryLineFor), so a backfilled memory reads like one written
    last night. Returns None for anything not worth remembering -- which is most of it."""
    try:
        d = json.loads(detail or "{}")
    except ValueError:
        d = {}
    name = d.get("name") or ""
    rank = int(d.get("rank") or 0)

    if kind == "kill":
        # Trash is excluded as the live path excludes it, and a dungeon's elites (rank 1) are its trash:
        # 130 of the 279 kills in the Deadmines run. Rank 3 is a world boss, 2 rare-elite, 4 rare.
        if d.get("boss") or rank == 3:
            return f"brought down {name}, the master of this place"
        if rank in (2, 4):
            return f"killed {name}"
        return None
    if kind == "death":
        return f"was killed by {name}" if name else "was killed"
    if kind == "quest_complete":
        title = d.get("title") or ""
        return f"finished the task '{title}'" if title else None
    if kind == "level_up":
        return f"grew stronger, now level {d.get('new')}" if d.get("new") else "grew stronger"
    if kind == "loot_item":
        return f"picked up {name}" if int(d.get("quality") or 0) >= 3 and name else None
    return None


def outings(args):
    """Every stretch of deeds a bot took part in, with who stood there and where it happened."""
    sys.path.insert(0, os.path.join(_SERVICES, "regard"))
    import regard as rg  # noqa: E402  (Parties replay, lands, instance names)

    zone_name = {z[0]: z[1] for z in rg.lands.CLASSIC_ZONES}
    names = {int(r[0]): r[1] for r in sql("SELECT guid, name FROM characters")}
    # Whose characters count as "you". With no --account this is every household that has named a main,
    # rather than one hardcoded id: on a realm with two people, defaulting to one of them meant the bots
    # structurally could not remember the other (plan 43 H4).
    accounts = [int(args.account)] if int(args.account) else [
        int(r[0]) for r in sql("SELECT account_id FROM player_main")]
    if not accounts:
        sys.exit("no household has named a main, so nobody is 'you': name one (GUIDE 10.5) or pass --account")
    players = {int(r[0]): r[1] for r in
               sql("SELECT guid, name FROM characters WHERE account IN ("
                   + ",".join(str(a) for a in accounts) + ")")}
    if not players:
        sys.exit(f"no characters on account(s) {', '.join(str(a) for a in accounts)}")
    log(f"the people: {', '.join(sorted(players.values()))}")

    where = ""
    if args.since:
        where += f" AND e.ts >= {q(args.since)}"
    if args.until:
        where += f" AND e.ts < {q(args.until)}"
    rows = sql("SELECT UNIX_TIMESTAMP(e.ts), e.actor_guid, e.event_type, e.zone_id, e.map_id, "
               "REPLACE(COALESCE(e.detail, ''), '\\t', ' ') FROM ledger_event e "
               f"WHERE e.event_type IN ({','.join(q(k) for k in NOTABLE)}){where} ORDER BY e.id")

    deeds = []
    for r in rows:
        line = deed_of(r[2], "\t".join(r[5:]))
        if line:
            deeds.append(dict(ts=float(r[0]), guid=int(r[1]), zone=int(r[3]), map=int(r[4]), line=line))
    log(f"{len(deeds)} notable deeds out of {len(rows)} events in the window")
    if not deeds:
        return []

    parties = rg.Parties()
    by_actor = {}
    for d in deeds:
        by_actor.setdefault(d["guid"], []).append(d)

    out = []
    for guid, mine in by_actor.items():
        if guid in players:
            continue                      # the operator's own characters keep no bot memories
        blocks, cur, last = [], [], None
        for d in mine:
            if last is not None and d["ts"] - last > OUTING_GAP_MINUTES * 60:
                blocks.append(cur)
                cur = []
            cur.append(d)
            last = d["ts"]
        if cur:
            blocks.append(cur)

        for block in blocks:
            start, end = block[0]["ts"], block[-1]["ts"]
            company = set()
            for d in block:
                company |= parties.standing_with(guid, d["ts"])
            company.discard(guid)

            # Whose deeds this bot would have seen: its own, and those of whoever stood with it.
            witnessed = [d for d in deeds
                         if start - 120 <= d["ts"] <= end + 120 and (d["guid"] == guid or d["guid"] in company)]
            if len(witnessed) < MIN_DEEDS:
                continue
            if not args.all and not (company & set(players)):
                continue                  # only outings the operator was actually part of

            # Cities, and the sub-zones inside an instance, are not in the zone list at all -- 14 of the 53
            # (zone, map) pairs in a week resolve to nothing. Taking the most common pair outright can
            # therefore land on one with no name. Rank by frequency but take the commonest that actually
            # resolves, so an evening in the Wetlands and its harbour is remembered as the Wetlands rather
            # than as "the wilds".
            def named(pair):
                return rg.instance_name(pair[0]) or zone_name.get(pair[1])

            pairs = sorted({(d["map"], d["zone"]) for d in witnessed},
                           key=lambda k: -sum(1 for d in witnessed if (d["map"], d["zone"]) == k))
            place = next((named(p) for p in pairs if named(p)), "the wilds")

            # A task turned in by a party writes one ledger row per member, so the same deed arrives four
            # times under four names and eats the whole budget. Collapsing them is not merely tidier:
            # "the company finished the task" is the thing actually worth remembering, and it is the
            # sentence that answers what we did together.
            merged, who = [], {}
            for d in witnessed:
                key = (d["line"], int(d["ts"] // 60))
                if key in who:
                    who[key].add(d["guid"])
                    continue
                who[key] = {d["guid"]}
                merged.append((key, d))

            # Each deed carries its own place, the way the module appends one live (events.cpp). Without
            # this the model is told what happened and never where, which is half of what plan 38 is for --
            # and the outing-level `place` below never reaches the prompt at all, it only labels the dry
            # run. Per-deed is also truer than per-outing: a quest handed in at a city gate belongs to the
            # marsh it was worked in, and saying so beats asserting one place for the whole evening.
            def placed(d):
                p = named((d["map"], d["zone"]))
                return f", in {p}" if p else ""

            kept = merged[-MAX_DEEDS_PER_PROMPT:]
            text = "".join(
                f" - {'the company' if len(who[k]) > 1 else names.get(d['guid'], 'someone')} "
                f"{d['line']}{placed(d)}\n"
                for k, d in kept)
            out.append(dict(guid=guid, name=names.get(guid, "someone"), at=end, deeds=len(kept), place=place,
                            company=", ".join(sorted(names.get(g, "someone") for g in company)) or
                                    "no one; you were alone",
                            events=text))
    return out


def events_backfill(args):
    outs = outings(args)
    if not outs:
        log("nothing to digest in that window")
        return 0
    # Already written? An outing's end time becomes the row's created_at, so running the same window twice
    # writes a whole second generation of every outing: six more memories saying the same things in other
    # words. That is not caught by any duplicate test, because two generations rarely share a phrase. It
    # happened on the first run -- Kinthea ended with twelve rows at one timestamp, two descending sets of
    # six -- and it matters because MaxPerBot is 40 and she reached 39.
    done = {(int(r[0]), int(r[1])) for r in
            sql("SELECT bot_guid, UNIX_TIMESTAMP(created_at) FROM mod_ollama_chat_memories")}
    already = [o for o in outs if (o["guid"], int(o["at"])) in done]
    if already:
        log(f"skipping {len(already)} outings already digested "
            f"({', '.join(sorted({o['name'] for o in already}))})")
        outs = [o for o in outs if (o["guid"], int(o["at"])) not in done]
    if not outs:
        log("every outing in that window is already remembered; nothing to do")
        return 0

    by_bot = {}
    for o in outs:
        by_bot.setdefault(o["guid"], []).append(o)
    log(f"{len(outs)} outings across {len(by_bot)} bots "
        f"(split on a {OUTING_GAP_MINUTES} min lull)")

    have = {int(r[0]): int(r[1]) for r in
            sql("SELECT bot_guid, COUNT(*) FROM mod_ollama_chat_memories GROUP BY bot_guid")}
    template = event_prompt()

    if args.dry_sample:
        o = max(outs, key=lambda o: o["deeds"])
        print(template.format(bot_name=o["name"], company=o["company"], events=o["events"])[:3000])
        return 0
    if not args.apply:
        for guid, os_ in sorted(by_bot.items(), key=lambda kv: -sum(o["deeds"] for o in kv[1])):
            log(f"  {os_[0]['name']:<14} {len(os_)} outings, {sum(o['deeds'] for o in os_)} deeds, "
                f"{have.get(guid, 0)} memories now  [{'; '.join(sorted({o['place'] for o in os_}))}]")
        log("dry run; pass --apply to write")
        return 0

    lanes = [l for l in fleet.prefer_lanes(args.lanes, args.slots) if "traits" in l.kinds]
    written = []

    def job(o):
        def run(lane, attempt):
            text = lane.chat([{"role": "user",
                               "content": template.format(bot_name=o["name"], company=o["company"],
                                                          events=o["events"])}],
                             400, temperature=0.7)
            got = parse_memories(text.translate(ASCII_PUNCT))
            if not got:
                return "nothing worth keeping"
            written.append((o, got))
            return f"{len(got)} from {o['deeds']} deeds in {o['place']}"
        return run

    pool = fleet.Pool(lanes, log=lambda m: log(m) if "FAILED" in m or "pool finished" in m else None)
    for o in outs:
        pool.submit("traits", 1, job(o), f"deeds {o['guid']} {o['name']} @{int(o['at'])}")
    failures = pool.run()

    values, kept = [], {}
    for o, got in sorted(written, key=lambda w: w[0]["at"]):
        for importance, body in got:
            n = kept.get(o["guid"], have.get(o["guid"], 0))
            if n >= MAX_PER_BOT:
                continue
            kept[o["guid"]] = n + 1
            values.append(f"({o['guid']}, {q(body[:400])}, {importance}, FROM_UNIXTIME({int(o['at'])}))")
    if values:
        for i in range(0, len(values), 200):
            sql("INSERT INTO mod_ollama_chat_memories (bot_guid, memory_text, importance, created_at) "
                "VALUES " + ",".join(values[i:i + 200]), fetch=False)
    log(f"wrote {len(values)} memories for {len(kept)} bots; {len(failures)} outings failed")
    log("these rows are only safe while the worldserver is DOWN: Memory_SaveAll deletes and reinserts "
        "every dirty bot's rows from RAM, and Memory_Load reads the table at startup only")
    return 1 if failures else 0


# The first run of event memories (plan 38) went fleet-wide before it was scoped to companions and wrote
# 892 rows in thirteen minutes. A third of them are not memories at all: written in the first person, stubs
# left behind by a truncated answer ("The cost", "The memory"), or a note about having been nobody's
# companion that evening -- the prompt's own "no one; you were alone" handed back as a thing to remember.
JUNK_MEMORY = re.compile(r"^(i|my|we)\s|\balone\b|solitude|no ally|no one stood", re.I)


def prune_events(args):
    """Drop that shape inside a time window.

    MUST run while the worldserver is stopped. Memory_SaveAll deletes and reinserts every dirty bot's rows
    from RAM, so a row deleted underneath a running realm comes straight back the next time that bot forms
    a memory -- and the shutdown save does it too, which is why this cannot simply precede a restart.
    """
    rows = sql("SELECT m.id, c.name, m.importance, REPLACE(m.memory_text, '\\t', ' ') "
               "FROM mod_ollama_chat_memories m JOIN characters c ON c.guid = m.bot_guid "
               f"WHERE m.created_at >= {q(args.since)} AND m.created_at <= {q(args.until)} "
               "ORDER BY m.id")
    doomed = []
    for r in rows:
        text = "\t".join(r[3:])
        if len(text.split()) < 4 or JUNK_MEMORY.search(text):
            doomed.append(r)

    log(f"{len(rows)} memories in the window; {len(doomed)} are first-person, stubs, or about solitude")
    for r in doomed[:8]:
        log(f"  {r[1]:<14} {r[2]:>2}  {' '.join(r[3:])[:90]}")
    if len(doomed) > 8:
        log(f"  ... and {len(doomed) - 8} more")
    if not doomed:
        return 0
    if not args.apply:
        log("dry run; pass --apply to delete")
        return 0

    for i in range(0, len(doomed), 500):
        ids = ",".join(r[0] for r in doomed[i:i + 500])
        sql(f"DELETE FROM mod_ollama_chat_memories WHERE id IN ({ids})", fetch=False)
    log(f"deleted {len(doomed)}; {len(rows) - len(doomed)} kept from that window")
    return 0


def prune_dupes(args):
    """Drop the second generation, if the backfill was run twice over the same window.

    The discriminator is the importance column, not the row count. Each generation emits ONE descending
    run -- 10, 9, 8, 7, 6, 5 -- so an importance repeated inside a single (bot, timestamp) group is the
    second run talking. Row count cannot be used: the module's own condenser writes up to twelve memories
    in a batch, so "more than six" would delete real condensation memories from other days.

    Like the other write commands, only durable with the worldserver stopped.
    """
    outs = outings(args)
    if not outs:
        log("no outings in that window")
        return 0

    pairs = ",".join(f"({o['guid']},{int(o['at'])})" for o in outs)
    rows = sql("SELECT id, bot_guid, UNIX_TIMESTAMP(created_at), importance, "
               "REPLACE(memory_text, '\\t', ' ') FROM mod_ollama_chat_memories "
               f"WHERE (bot_guid, UNIX_TIMESTAMP(created_at)) IN ({pairs}) "
               "ORDER BY bot_guid, created_at, id")
    names = {o["guid"]: o["name"] for o in outs}

    seen, doomed = {}, []
    for r in rows:
        key, imp = (int(r[1]), int(r[2])), int(r[3])
        got = seen.setdefault(key, set())
        if imp in got:
            doomed.append(r)
        else:
            got.add(imp)

    log(f"{len(rows)} rows across {len(seen)} outings; {len(doomed)} are a second generation")
    for r in doomed[:8]:
        log(f"  {names.get(int(r[1]), '?'):<14} {r[3]:>2}  {' '.join(r[4:])[:88]}")
    if len(doomed) > 8:
        log(f"  ... and {len(doomed) - 8} more")
    if not doomed:
        return 0
    if not args.apply:
        log("dry run; pass --apply to delete")
        return 0

    for i in range(0, len(doomed), 500):
        ids = ",".join(r[0] for r in doomed[i:i + 500])
        sql(f"DELETE FROM mod_ollama_chat_memories WHERE id IN ({ids})", fetch=False)
    log(f"deleted {len(doomed)}; {len(rows) - len(doomed)} kept")
    return 0


def show(args):
    rows = sql("SELECT c.name, m.importance, REPLACE(m.memory_text, '\\t', ' '), m.created_at "
               "FROM mod_ollama_chat_memories m JOIN characters c ON c.guid = m.bot_guid "
               + (f"WHERE c.name = {q(args.bot)} " if args.bot else "")
               + "ORDER BY m.bot_guid, m.importance DESC, m.id LIMIT 200")
    for r in rows:
        print(f"{r[0]:<14} {r[1]:>2}  {' '.join(r[2:-1])[:120]}")
    print(f"-- {len(rows)} rows")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prune", help="drop memories that say only that the bot said nothing")
    p.add_argument("--apply", action="store_true")
    b = sub.add_parser("backfill", help="condense past conversations from ledger_chat into memories")
    b.add_argument("--since", type=int, default=0, help="only the last N days (0: everything)")
    b.add_argument("--lanes", default="evo-quality,z13-qwen35")
    b.add_argument("--slots", default="")
    b.add_argument("--apply", action="store_true")
    b.add_argument("--dry-sample", action="store_true", help="print one built prompt and stop")
    e = sub.add_parser("events", help="digest past deeds from ledger_event into memories (plan 38)")
    e.add_argument("--since", default="", help="start of the window, e.g. '2026-09-18 00:00:00'")
    e.add_argument("--until", default="", help="end of the window (exclusive)")
    e.add_argument("--account", type=int, default=0,
                   help="one account whose characters are 'you'; default every household that has named a main")
    e.add_argument("--all", action="store_true", help="every bot, not only those who travelled with you")
    e.add_argument("--lanes", default="evo-quality,z13-qwen35")
    e.add_argument("--slots", default="")
    e.add_argument("--apply", action="store_true")
    e.add_argument("--dry-sample", action="store_true", help="print one built prompt and stop")
    pe = sub.add_parser("prune-events", help="drop the junk the fleet-wide first run wrote (server must be down)")
    pe.add_argument("--since", default="2026-09-21 23:11:11")
    pe.add_argument("--until", default="2026-09-21 23:38:52")
    pe.add_argument("--apply", action="store_true")
    pd = sub.add_parser("prune-dupes", help="drop a second generation left by running events twice")
    pd.add_argument("--since", default="2026-09-18 00:00:00")
    pd.add_argument("--until", default="2026-09-21 23:11:11")
    pd.add_argument("--account", type=int, default=0)
    pd.add_argument("--all", action="store_true")
    pd.add_argument("--apply", action="store_true")
    s = sub.add_parser("show")
    s.add_argument("--bot")
    args = ap.parse_args()
    return dict(prune=prune, backfill=backfill, events=events_backfill,
                **{"prune-events": prune_events, "prune-dupes": prune_dupes}, show=show)[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
