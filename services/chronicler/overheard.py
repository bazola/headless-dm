"""Overheard: where each rumour travelled, and the lines of chat where someone passed it on (dashboard Rumours).

The chronicler knows where a tale is told (chronicle_rumour: a land, a side, until when), but not whether anyone
ever said it: mod-ollama-chat puts one rumour into a prompt now and then and logs nothing about which. So the
evidence here is inferred from ledger_chat. A bot's line counts against the rumours going around its land, for
its side, at the moment it spoke, and is kept when it shares the tale's rare words -- names above all, weighted by
how rarely they turn up in chat at all, so "Araj" counts and "rogue" hardly does.

Place and company names are no evidence: every bot's prompt already names its land and the companies around it.
Two tiers:
  passed  it says it is passing word on ("rumour says", "they say", "I heard") and matches a tale told there
  echo    no such words, but a strong match on the tale's names, away from the land where it happened (where it
          happened, the speaker may simply have been there)
A third, "carried" -- words of passing on that match a tale told somewhere else -- was tried and dropped
(2026-09-26): 27 lines against 23 by chance, which is no finding at all.
Each tier is also scored against tales that were NOT going around there (the same number of them, drawn at
random), and that count is published beside it as `chance`: what the matcher finds with no rumour to find.
Recurring foes put some real news in that control -- a second telling of the same captain's death -- so it
overstates chance a little, never understates it.
"""
import collections
import datetime as dt
import json
import math
import os
import random
import re
import time

STORY_DAYS = 4          # stories whose first telling is this recent are published
PASSED_SCORE = 6.0
ECHO_SCORE = 10.0
OTHER_WEIGHT = 0.3      # a shared word that is not a name counts this much of its rarity
MARKER = re.compile(r"\b(rumou?rs?|they say|they're saying|folk say|word is|word has it|word came|word from|"
                    r"i hear|i heard|heard tell|heard|news of|tales? of|talk of|whispers? of)\b", re.I)
STOP = set("the a an and but or of in on at to from by with for as is was were be been are it its his her their they "
           "them he she we i you my your our this that these those there here not no so if then than when who what "
           "which one two three more once again yet still only all each every some any into over under out up down "
           "said say says".split())
WORD = re.compile(r"[A-Za-z][A-Za-z'-]+")

_corpus = dict(last=0, n=0, df=collections.Counter())   # document frequency over every bot line, grown by cursor


def words(s):
    """Lower-cased, and a possessive is its owner: "Ashenvale's" is Ashenvale."""
    return [re.sub(r"'s$", "", w.lower()) for w in WORD.findall(s)]


def content(s):
    return {w for w in words(s) if w not in STOP and len(w) > 2}


def vocatives(text):
    """Names spoken to rather than about: "Hecateri, ..." or "..., Emidan." """
    return {m.lower() for m in re.findall(r"(?:^|[.!?]\s+)([A-Z][\w'-]+),", text)} \
        | {m.lower() for m in re.findall(r",\s*([A-Z][\w'-]+)[.!?]*\s*$", text)} \
        | {m.lower() for m in re.findall(r",\s*([A-Z][\w'-]+)[.!?]", text)}


def grow_corpus(sql):
    for i, hx in sql(f"SELECT id, HEX(text) FROM ledger_chat WHERE speaker_is_bot = 1 AND id > {_corpus['last']} "
                     "ORDER BY id"):
        _corpus["df"].update(set(words(bytes.fromhex(hx).decode(errors="replace"))))
        _corpus["n"] += 1
        _corpus["last"] = int(i)


def build(sql, place_name, side_of, neighbours, names_in, known_words, common, horde_races):
    """known_words: the words of every place and company name, which no line gets credit for sharing."""
    grow_corpus(sql)
    n, df = max(1, _corpus["n"]), _corpus["df"]

    def idf(w):
        return math.log(n / (1 + df[w]))

    since = dt.datetime.now() - dt.timedelta(days=STORY_DAYS)
    since_s = since.strftime("%Y-%m-%d %H:%M:%S")
    tales = {}
    for tid, root, parent, entry, team, kind, hop, origin, hx, told in sql(
            "SELECT t.id, COALESCE(t.root_id, t.id), COALESCE(t.parent_id, 0), t.entry_id, t.team, t.kind, t.hop, "
            "t.origin, HEX(t.words), UNIX_TIMESTAMP(t.told_at) FROM chronicle_tale t "
            "JOIN chronicle_tale r ON r.id = COALESCE(t.root_id, t.id) "
            f"WHERE r.told_at >= '{since_s}' ORDER BY t.id"):
        text = bytes.fromhex(hx).decode(errors="replace")
        tales[int(tid)] = dict(id=int(tid), root=int(root), parent=int(parent) or None, entry=int(entry),
                               team=int(team), kind=kind, hop=int(hop), origin=int(origin), words=text,
                               told=int(float(told)), places=[], until=0,
                               _names={words(w)[0] for w in names_in(text) if words(w)} - known_words - common,
                               _content=content(text) - common - known_words)
    if not tales:
        return dict(generated=int(time.time()), stories=[], evidence=[], roads=[], places={}, stats={})

    active = collections.defaultdict(list)     # (zone, team) -> [(from, until, tale)]
    for tid, zone, team, until in sql(
            "SELECT tale_id, zone_id, team, UNIX_TIMESTAMP(expires_at) FROM chronicle_rumour "
            f"WHERE tale_id IN ({','.join(map(str, tales))}) ORDER BY id"):
        t = tales.get(int(tid))
        if not t:
            continue
        zone, until = int(zone), int(float(until))
        if zone not in t["places"]:
            t["places"].append(zone)
        t["until"] = max(t["until"], until)
        active[(zone, int(team))].append((t["told"], until, t["id"]))

    entries = {int(i): (title, f) for i, title, f in sql(
        "SELECT id, title, faction FROM chronicle_entry WHERE id IN "
        f"({','.join(str(i) for i in {t['entry'] for t in tales.values()})})")}

    lines = [(int(i), int(float(ts)), int(g), name, int(z), bytes.fromhex(hx).decode(errors="replace"))
             for i, ts, g, name, z, hx in sql(
                 "SELECT id, UNIX_TIMESTAMP(ts), speaker_guid, speaker_name, zone_id, HEX(text) FROM ledger_chat "
                 f"WHERE speaker_is_bot = 1 AND ts >= '{since_s}' ORDER BY id")]
    guids = sorted({l[2] for l in lines})
    team_of = {}
    for part in range(0, len(guids), 2000):
        chunk = guids[part:part + 2000]
        team_of.update({int(g): 1 if int(r) in horde_races else 0 for g, r in sql(
            f"SELECT guid, race FROM characters WHERE guid IN ({','.join(map(str, chunk))})")})

    def score(text, speaker, t):
        mine = content(text)
        shared = mine & t["_content"]
        named = (shared & t["_names"]) - vocatives(text) - {speaker.lower()}
        rest = shared - t["_names"]
        s = sum(idf(w) for w in named) + OTHER_WEIGHT * sum(idf(w) for w in rest)
        return s, sorted(named, key=lambda w: -idf(w)), sorted(rest, key=lambda w: -idf(w))

    def best(text, speaker, ids):
        top = (0.0, None, [], [])
        for tid in ids:
            s, named, rest = score(text, speaker, tales[tid])
            if s > top[0]:
                top = (s, tid, named, rest)
        return top

    def tier(s, named, marked, away):
        if marked and s >= PASSED_SCORE:
            return "passed"
        if not marked and away and named and s >= ECHO_SCORE:
            return "echo"
        return None

    rng = random.Random(19)
    all_ids = list(tales)
    evidence, chance = [], collections.Counter()
    in_reach = marked_lines = 0
    for lid, ts, guid, name, zone, text in lines:
        team = team_of.get(guid)
        if team is None:
            continue
        marked = bool(MARKER.search(text))
        marked_lines += marked
        here = [tid for start, until, tid in active.get((zone, team), ()) if start <= ts <= until]
        held = set(here)
        if not here:
            continue
        in_reach += 1
        found = None
        s, tid, named, rest = best(text, name, here)
        kind = tier(s, named, marked, tid is not None and tales[tid]["origin"] != zone)
        if kind:
            found = (kind, s, tid, named, rest)
        # The control: as many tales that were not going around here, drawn at random, held to the same test.
        pool = [x for x in rng.sample(all_ids, min(len(all_ids), len(here) * 3 + 10)) if x not in held][:len(here)]
        cs, ctid, cnamed, _ = best(text, name, pool)
        ctier = tier(cs, cnamed, marked, ctid is not None and tales[ctid]["origin"] != zone)
        if ctier:
            chance[ctier] += 1
        if found:
            kind, s, tid, named, rest = found
            evidence.append(dict(id=lid, ts=ts, guid=guid, name=name, zone=zone, place=place_name(zone) or "",
                                 text=text, tale=tid, root=tales[tid]["root"], hop=tales[tid]["hop"], tier=kind,
                                 score=round(s, 1), names=named[:6], words=rest[:6]))

    heard = collections.Counter(e["tale"] for e in evidence)
    roots = collections.defaultdict(list)
    for t in tales.values():
        roots[t["root"]].append(t)
    stories = []
    for root, ts_ in roots.items():
        ts_.sort(key=lambda t: (t["hop"], t["id"]))
        first = ts_[0]
        title, faction = entries.get(first["entry"], ("", ""))
        places = []
        for t in ts_:
            for z in t["places"]:
                if z not in places:
                    places.append(z)
        stories.append(dict(
            root=root, entry=first["entry"], watch=title, team=first["team"],
            faction="Horde" if first["team"] == 1 else "Alliance", kind=first["kind"], origin=first["origin"],
            origin_name=place_name(first["origin"]) or "", told=first["told"],
            until=max(t["until"] for t in ts_), hops=max(t["hop"] for t in ts_), places=places,
            heard=sum(heard[t["id"]] for t in ts_),
            tellings=[dict(id=t["id"], parent=t["parent"], hop=t["hop"], words=t["words"], told=t["told"],
                           until=t["until"], places=t["places"], heard=heard[t["id"]]) for t in ts_]))
    stories.sort(key=lambda s: -s["told"])

    zones = {z for s in stories for z in s["places"]} | {s["origin"] for s in stories} | {e["zone"] for e in evidence}
    roads = sorted({tuple(sorted((a, b))) for a in zones for b in neighbours.get(a, ()) if b in zones})
    now = int(time.time())
    counts = collections.Counter(e["tier"] for e in evidence)
    return dict(
        generated=now, since=int(since.timestamp()),
        stories=stories,
        evidence=sorted(evidence, key=lambda e: -e["ts"]),
        roads=[list(r) for r in roads],
        places={str(z): dict(name=place_name(z) or "", side=side_of(z) or "") for z in sorted(zones)},
        stats=dict(lines=len(lines), in_reach=in_reach, marked=marked_lines,
                   going_now=sum(1 for s in stories if s["until"] > now),
                   passed=counts["passed"], echo=counts["echo"],
                   chance=dict(passed=chance["passed"], echo=chance["echo"]),
                   thresholds=dict(passed=PASSED_SCORE, echo=ECHO_SCORE)))


def write(path, doc):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, ensure_ascii=False)
    os.replace(tmp, path)
