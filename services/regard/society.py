#!/usr/bin/env python3
"""Bot society seeds: the feelings bots start with toward one another.

custom wow plans/18, step P8b (operator decisions S1, S2). Regard only learns from what happens (plan 14), so on a
fresh world bots hardly know each other and companies have no attachment to act on. One-off seeds fix that:

  fellowship  every two members of the same company get a regard row unless they already have one. The company's
              warmth (+20) varies by pair, a little more when two share a temperament or a kind of motivation, and a
              few members (about one in ten) never quite fit: they feel 25 less toward the rest, who feel 10 less
              toward them. The seeded value is also the row's baseline, so it lasts instead of decaying back to +20.
  lore        a backstory that names a fellow member ("say what is between you") becomes that feeling: the utility
              lane reads the lines naming them and says how the writer feels (-40..+40) and one sentence in their own
              words. Stored with familiarity 3, as baseline and as the regard sentence.

Presence (regard.py, P8a) then moves these day by day. Nothing here touches game objects.
Standing rule: bots are people living in Azeroth. Everything written is in-world.

Usage:
  python3 society.py fellowship [--dry]
  python3 society.py lore [--dry] [--limit N]
  python3 society.py show
"""
import argparse
import concurrent.futures as cf
import hashlib
import json
import re
import sys

import regard as rg  # also puts /opt/wow/lore on the path
import era as eras  # noqa: E402
import fleet  # noqa: E402

log = rg.log
sql, q = rg.sql, rg.q

WARMTH = rg.BASE_SAME_GUILD
PAIR_SPREAD = 8          # symmetric part of a pair's variation, +-
WAY_SPREAD = 4           # one way's own part, +-
SAME_TEMPERAMENT = 4
SAME_MOTIVATION = 3
OUTSIDER_SHARE = 10      # percent of members who never quite fit
OUTSIDER_OUT = -25.0     # an outsider's feeling toward the rest ...
OUTSIDER_IN = -10.0      # ... and theirs toward the outsider
LORE_FAMILIARITY = 3
LORE_SLOTS = 2

LORE_PROMPT = """These lines are from the life story of {writer}, told to them in the second person ("you"). They mention {named}, who rides in the same company:
{lines}

How does {writer} feel about {named} now? Reply with JSON only:
{{"feels": <a whole number from -40 (hatred, fear, bitter rivalry) to 40 (deep trust, love, gratitude)>, "thought": "one sentence of at most 25 words in the second person: how you feel about {named} and why. In-world, no numbers."}}"""


def spread(text, width):
    """A stable number in -width..width from text."""
    return int(hashlib.md5(text.encode()).hexdigest()[:8], 16) % (2 * width + 1) - width


def members():
    """{guildid: [(guid, name, temperament, motivation kind)]}, bots only."""
    out = {}
    for gid, guid, temper, kind, name in sql(
            "SELECT m.guildid, m.guid, COALESCE(l.temperament, ''), COALESCE(l.motivation_kind, ''), c.name "
            "FROM guild_member m JOIN characters c ON c.guid = m.guid "
            "JOIN person_kind pk ON pk.guid = c.guid AND pk.kind IN ('bot', 'alt') "
            "LEFT JOIN lore_character l ON l.guid = m.guid ORDER BY m.guildid, m.guid"):
        out.setdefault(int(gid), []).append((int(guid), name, temper, kind))
    return out


def fellowship_score(gid, a, b):
    """How a feels toward b, both members of company gid: (score, reason)."""
    ga, gb = a[0], b[0]
    score = WARMTH + spread(f"pair:{min(ga, gb)}:{max(ga, gb)}", PAIR_SPREAD) + spread(f"way:{ga}>{gb}", WAY_SPREAD)
    if a[2] and a[2] == b[2]:
        score += SAME_TEMPERAMENT
    if a[3] and a[3] == b[3]:
        score += SAME_MOTIVATION
    reason = "sworn to the same company"
    if outsider(gid, ga):
        score += OUTSIDER_OUT
        reason = "you have never quite belonged among the company"
    elif outsider(gid, gb):
        score += OUTSIDER_IN
        reason = "they have never quite fit in with the company"
    return max(-100.0, min(100.0, float(score))), reason


def outsider(gid, guid):
    return spread(f"outsider:{gid}:{guid}", 50) + 50 < OUTSIDER_SHARE


def fellowship(args):
    companies = members()
    rows, outsiders = [], 0
    for gid, people in companies.items():
        outsiders += sum(1 for p in people if outsider(gid, p[0]))
        for a in people:
            for b in people:
                if a[0] != b[0]:
                    score, reason = fellowship_score(gid, a, b)
                    rows.append((a[0], b[0], score, reason))
    log(f"fellowship: {len(rows)} feelings in {len(companies)} companies, {outsiders} members who never quite fit")
    if args.dry:
        for gid, people in list(companies.items())[:1]:
            for a in people[:3]:
                for b in people[:4]:
                    if a[0] != b[0]:
                        print(f"   {a[1]} -> {b[1]}: {fellowship_score(gid, a, b)}")
        attach = {}
        for bot, _, score, _ in rows:
            attach.setdefault(bot, []).append(score)
        means = sorted(sum(v) / len(v) for v in attach.values())
        print(f"   attachment: lowest {means[0]:.1f}, below 0: {sum(1 for m in means if m < 0)}, median {means[len(means) // 2]:.1f}")
        return 0
    before = int(sql("SELECT COUNT(*) FROM regard")[0][0])
    for part in rg.chunks(rows, 500):
        # IGNORE: a feeling already earned stays as it is.
        sql("INSERT IGNORE INTO regard (bot_guid, other_guid, score, baseline, familiarity, last_reason) VALUES "
            + ",".join(f"({a},{b},{s:.2f},{s:.2f},2,{q(r)})" for a, b, s, r in part), fetch=False)
    log(f"fellowship: {int(sql('SELECT COUNT(*) FROM regard')[0][0]) - before} rows added")
    return 0


def lore_jobs():
    """[(writer guid, writer name, named guid, named name, lines)] for backstories naming a fellow member without a
    regard sentence yet."""
    companies = members()
    described = {(int(r[0]), int(r[1])) for r in sql("SELECT bot_guid, other_guid FROM regard WHERE description IS NOT NULL "
                                                     "AND description <> ''")}
    stories = {int(r[0]): r[1] for r in sql(
        "SELECT guid, REPLACE(REPLACE(backstory, CHAR(10), ' '), CHAR(9), ' ') FROM lore_character")}
    jobs = []
    for people in companies.values():
        for writer, wname, _, _ in people:
            story = stories.get(writer, "")
            if not story:
                continue
            sentences = re.split(r"(?<=[.!?])\s+", story)
            for named, nname, _, _ in people:
                if named == writer or (writer, named) in described:
                    continue
                lines = [s for s in sentences if re.search(rf"\b{re.escape(nname)}\b", s)]
                if lines:
                    jobs.append((writer, wname, named, nname, " ".join(lines)[:900]))
    return jobs


def lore(args):
    # Was eras.get("classic"), which ignored --era outright: asking for another era ran Classic anyway.
    e = eras.get(getattr(args, "era", None) or rg.site.get("ERA", "classic"))
    jobs = lore_jobs()
    if args.limit:
        jobs = jobs[:args.limit]
    log(f"lore: {len(jobs)} named relations to judge")
    # The judge's address lives in site/fleet.toml, not in three files that could disagree (23 W3).
    _j = fleet._fleet.judge()
    judge = fleet.Lane(_j["name"], _j["url"], _j["model"], LORE_SLOTS, set(), timeout=90,
                       lmstudio=_j["lmstudio"])
    baselines = {(int(r[0]), int(r[1])): float(r[2]) for r in sql("SELECT bot_guid, other_guid, baseline FROM regard")}

    def ask(job):
        writer, wname, named, nname, lines = job
        prompt = LORE_PROMPT.format(writer=wname, named=nname, lines=lines)
        for _ in range(3):
            try:
                out = judge.chat([{"role": "user", "content": prompt}], 200, temperature=0.2, json_mode=True)
                m = re.search(r"\{.*\}", out, re.S)
                data = json.loads(m.group(0)) if m else {}
                feels = max(-40, min(40, int(data.get("feels"))))
                thought = re.sub(r"\s+", " ", str(data.get("thought") or "")).strip().strip('"').translate(rg.ASCII_PUNCT)
                if not thought.lower().startswith("you") or len(thought.split()) > 40 or e["meta"].search(thought):
                    raise ValueError(f"unusable thought {thought[:60]!r}")
                return job, feels, thought
            except (RuntimeError, ValueError, TypeError) as ex:
                err = ex
        return job, None, str(err)

    done = failed = 0
    with cf.ThreadPoolExecutor(LORE_SLOTS) as pool:
        for (writer, wname, named, nname, lines), feels, thought in pool.map(ask, jobs):
            if feels is None:
                failed += 1
                log(f"lore: {wname} about {nname} failed: {thought}")
                continue
            score = max(-100.0, min(100.0, baselines.get((writer, named), WARMTH) + feels))
            if args.dry:
                print(f"   {wname} -> {nname}: {feels:+d} ({score:.0f}) {thought}\n      from: {lines[:160]}")
                continue
            sql("INSERT INTO regard (bot_guid, other_guid, score, baseline, familiarity, last_reason, description, "
                f"described_score) VALUES ({writer}, {named}, {score:.2f}, {score:.2f}, {LORE_FAMILIARITY}, "
                f"{q(thought[:160])}, {q(thought[:255])}, {score:.2f}) ON DUPLICATE KEY UPDATE score = VALUES(score), "
                "baseline = VALUES(baseline), familiarity = GREATEST(familiarity, VALUES(familiarity)), "
                "last_reason = VALUES(last_reason), description = VALUES(description), "
                "described_score = VALUES(described_score)", fetch=False)
            done += 1
    log(f"lore: {done} feelings stored, {failed} failed")
    return 0


def show(_args):
    print("regard rows:", sql("SELECT COUNT(*), SUM(ABS(score - baseline) >= 1), SUM(description IS NOT NULL) FROM regard"))
    print("attachment:", sql("SELECT ROUND(MIN(att), 1), SUM(att < 0), ROUND(AVG(att), 1), COUNT(*) FROM (SELECT r.bot_guid, AVG(r.score) att "
                             "FROM regard r JOIN guild_member mb ON mb.guid = r.bot_guid JOIN guild_member mo "
                             "ON mo.guid = r.other_guid AND mo.guildid = mb.guildid GROUP BY r.bot_guid) x"))
    print("presence rows (last reason):", sql("SELECT COUNT(*) FROM regard WHERE last_reason LIKE 'crossed paths%' "
                                               "OR last_reason LIKE 'rode together%' OR last_reason LIKE 'fought side by side%'"))
    for r in sql("SELECT b.name, o.name, ROUND(r.score), COALESCE(r.description, '') FROM regard r JOIN characters b ON b.guid = r.bot_guid "
                 "JOIN characters o ON o.guid = r.other_guid WHERE r.description IS NOT NULL ORDER BY r.updated_at DESC LIMIT 5"):
        print(f"   {r[0]} -> {r[1]} ({r[2]}): {chr(9).join(r[3:])}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fellowship", help="a regard row for every two members of the same company")
    f.add_argument("--dry", action="store_true")
    l = sub.add_parser("lore", help="feelings from backstories that name a fellow member")
    l.add_argument("--dry", action="store_true")
    l.add_argument("--limit", type=int)
    sub.add_parser("show", help="counts and examples")
    args = ap.parse_args()
    return {"fellowship": fellowship, "lore": lore, "show": show}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
