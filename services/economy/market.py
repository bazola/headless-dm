#!/usr/bin/env python3
"""The market's memory (custom wow plans/17 §3.E): learned prices, market talk and the dashboard's market panel.

Reads mod-ledger's `ledger_auction` (listings, sales, expiries) and the live `auctionhouse` table. Each cycle:

1. Prices (`market_price`). For every item with a sale or a sold or expired merchant listing in the last
   WINDOW_HOURS, a target per-unit price is formed and the stored price moves ALPHA of the way toward it
   (once per sale, not once per cycle: sales already older than the stored price are in it already):
   - base: the median per-unit price people (players and townsfolk) paid, when there are two or more such sales;
     otherwise the stored price, else the median calculated merchant listing, else the one price paid;
   - demand: merchant stock that sells nudges it up, stock that sits unsold nudges it down (0.9-1.3);
   - supply: goods people sell to the merchants' buyer nudge it down (0.85-1.0);
   - never below what a vendor pays, never above 4x the calculated listing.
   The auction house module (AuctionHouseBot.MarketPrices.*) and bots posting their own loot read it. Prices of items
   with no events for STALE_DAYS are dropped.
2. Talk (`market_word`). Facts in words, no numbers, for each market city: goods piled high (and whose stalls), goods
   bought up as soon as they were set out, goods nobody wants, townsfolk selling their own finds. mod-ollama-chat
   (OllamaChat.Market.Talk) lets bots in those cities mention one.
3. Dashboard (`market.json` in the dashboard's data root): listings per house, the last day by hour, most traded
   goods, learned prices, recent sales. Operator page, so it has numbers.

Kinds: the merchants' account (MERCHANT_ACCOUNT) is "merchant", random bot accounts "bot", everyone else "player".
Kill switches (files next to this script): PAUSE (whole cycle), NO_PRICES (clears market_price), NO_WORDS (clears
market_word). Credentials come from site/secrets.env, else the live server config, and are never printed.
"""
import argparse
import collections
import json
import os
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Where this realm lives now comes from site/, with the live server config as the fallback (plan 23 W1/W16).
sys.path.insert(0, str(HERE.parent))
from common import site  # noqa: E402

MARKET_JSON = Path(os.environ.get("MARKET_JSON", os.path.join(
    site.get("DATA_DIR", "/opt/wow/server/data"), "dashboard-data", "market.json")))
PAUSE, NO_PRICES, NO_WORDS = HERE / "PAUSE", HERE / "NO_PRICES", HERE / "NO_WORDS"

MERCHANT_ACCOUNT = site.get("MERCHANT_ACCOUNT", "MERCHANTS")
WINDOW_HOURS = 72
ALPHA = 0.3
STALE_DAYS = 14
WORD_HOURS = 3
WORDS_PER_ZONE = 6

HOUSES = {2: "Alliance", 6: "Horde", 7: "Neutral"}
# house -> (team for market_word, [(zone id, city name, "in ..." form)])
MARKETS = {
    2: (0, [(1519, "Stormwind", "Stormwind"), (1537, "Ironforge", "Ironforge"), (1657, "Darnassus", "Darnassus")]),
    6: (1, [(1637, "Orgrimmar", "Orgrimmar"), (1638, "Thunder Bluff", "Thunder Bluff"),
            (1497, "Undercity", "the Undercity")]),
    7: (2, [(33, "Booty Bay", "Booty Bay"), (440, "Gadgetzan", "Gadgetzan"), (618, "Everlook", "Everlook")]),
}
COMMODITY_CLASSES = {0, 5, 7, 12}   # consumables, reagents, trade goods, quest goods: spoken of as goods, lowercase

# Each kind of market fact had exactly one phrasing, which is why 60% of all market talk shared
# the same seven-word run ("... is piled high on the stalls of the ..."). A pool per fact, drawn
# per line, gives the same news a different mouth in each city. No figures anywhere: market_word
# carries facts in words only. {n}/{T} are sentence-initial forms of {ln}/{t}.
GLUT_WORDS = (
    "{n} is piled high on the stalls of the {city} auction house",
    "The {city} stalls are heavy with {ln}",
    "You cannot move in the {city} auction house for {ln}",
    "There is more {ln} in {city} than the place knows what to do with",
    "Every other trader in {city} seems to be selling {ln}",
)
GLUT_FROM = (", much of it from {s}.", ", and {s} brought the bulk of it.", "; {s} has most of it.")
BOUGHT_WORDS = (
    "At the {city} auction house, {t} was bought up as soon as it was set out.",
    "{T} did not sit an hour on the {city} stalls before someone took it.",
    "Someone in {city} bought {t} the moment it appeared.",
    "{T} was taken off the {city} stalls before the day was out.",
)
UNSOLD_WORDS = (
    "Nobody in {into} seems to want {t} lately; it sits unsold at the auction house.",
    "{T} has been sitting unsold at the {city} auction house for days.",
    "You could not give away {t} in {into} this week.",
    "The {city} stalls still hold {t} that nobody will take.",
)
SELLING_WORDS = (
    "{s} has put {t} up for sale at the {city} auction house.",
    "{s} is asking after a buyer for {t} in {city}.",
    "{s} has {t} on the {city} stalls, for anyone who wants it.",
    "Word in {city} is that {s} is selling {t}.",
)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


_CHAR = site.db("characters")
AUTH_DB = site.db("auth")[4]
WORLD_DB = site.db("world")[4]


def sql(query, fetch=True):
    host, port, user, pw, db = _CHAR
    out = subprocess.run(["mysql", "--default-character-set=utf8mb4", "-h", host, "-P", port, "-u", user, db,
                          "--batch", "--raw", "-N", "-e", query],
                         env={"MYSQL_PWD": pw, "PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return [line.split("\t") for line in out.stdout.splitlines() if line] if fetch else None


def quote(text):
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def table_exists(name):
    return bool(sql(f"SELECT 1 FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = '{name}'"))


def kind_sql(guid_col):
    """SQL for the kind of the character in guid_col (needs no join from the caller)."""
    # The merchant test comes first on purpose: the merchants' account is nobody's bot and has no main,
    # so person_kind calls it 'player'. Everything else follows the view -- alts count as bots (plan 43 S1).
    return (f"(SELECT CASE WHEN a.username = '{MERCHANT_ACCOUNT}' THEN 'merchant' "
            f"WHEN pk.kind IN ('bot', 'alt') THEN 'bot' ELSE 'player' END "
            f"FROM characters c JOIN {AUTH_DB}.account a ON a.id = c.account "
            f"JOIN person_kind pk ON pk.guid = c.guid WHERE c.guid = {guid_col})")


# ---------------------------------------------------------------------------------------------------------------------
# reading

def load_events(hours):
    """ledger_auction rows of the window with seller and buyer kinds and names."""
    if not table_exists("ledger_auction"):
        return []
    rows = sql(
        "SELECT l.id, DATE_FORMAT(l.ts, '%Y-%m-%dT%H:%i:%s'), l.event, l.house_id, l.item_entry, l.item_count, "
        "l.buyout, l.price, l.owner_guid, COALESCE(l.bidder_guid, 0), "
        f"COALESCE({kind_sql('l.owner_guid')}, 'player'), COALESCE({kind_sql('l.bidder_guid')}, ''), "
        "COALESCE((SELECT name FROM characters WHERE guid = l.owner_guid), '') "
        f"FROM ledger_auction l WHERE l.ts >= NOW() - INTERVAL {int(hours)} HOUR ORDER BY l.id")
    return [dict(id=int(r[0]), ts=r[1], event=r[2], house=int(r[3]), entry=int(r[4]), count=max(1, int(r[5])),
                 buyout=int(r[6]), price=int(r[7]), owner=int(r[8]), bidder=int(r[9]), seller_kind=r[10],
                 buyer_kind=r[11], seller=r[12]) for r in rows]


def load_items(entries):
    if not entries:
        return {}
    ids = ",".join(map(str, sorted(entries)))
    return {int(r[0]): dict(name=r[1], quality=int(r[2]), cls=int(r[3]), sell=int(r[4])) for r in sql(
        f"SELECT entry, name, Quality, class, SellPrice FROM {WORLD_DB}.item_template WHERE entry IN ({ids})")}


def load_prices():
    if not table_exists("market_price"):
        return {}
    return {int(r[0]): dict(unit=int(r[1]), list=int(r[2]) if r[2] not in ("", "NULL") else None, at=r[3])
            for r in sql("SELECT item_entry, unit_copper, list_copper, "
                         "DATE_FORMAT(updated_at, '%Y-%m-%dT%H:%i:%s') FROM market_price")}


# ---------------------------------------------------------------------------------------------------------------------
# prices

def learn_prices(events, items, stored):
    """{entry: (unit, list_unit, samples)} for every item with evidence in the window."""
    by_item = collections.defaultdict(list)
    for e in events:
        by_item[e["entry"]].append(e)

    learned = {}
    for entry, evs in by_item.items():
        item = items.get(entry)
        if not item:
            continue
        listed = [e["buyout"] / e["count"] for e in evs if e["event"] == "list" and e["seller_kind"] == "merchant" and e["buyout"]]
        paid = [e["price"] / e["count"] for e in evs if e["event"] == "sale" and e["buyer_kind"] in ("player", "bot")]
        m_sold = sum(1 for e in evs if e["event"] == "sale" and e["seller_kind"] == "merchant")
        m_expired = sum(1 for e in evs if e["event"] == "expire" and e["seller_kind"] == "merchant")
        supplied = sum(1 for e in evs if e["event"] == "sale" and e["buyer_kind"] == "merchant")
        if not (paid or m_sold or supplied):
            continue    # nothing changed hands: the calculated price stands

        # A sale stays in the window for WINDOW_HOURS, but it must move the price once, not once a cycle. Without
        # this an item with a single sale drifts ALPHA toward its target every cycle until it sits on the vendor
        # floor: one Star Ruby sale took its price from 23768 to 5000 in eight hours.
        prior = stored.get(entry, {})
        newest_sale = max((e["ts"] for e in evs if e["event"] == "sale"), default=None)
        if prior.get("at") and newest_sale and newest_sale < prior["at"]:
            continue

        list_unit = statistics.median(listed) if listed else (stored.get(entry, {}).get("list"))
        old = stored.get(entry, {}).get("unit")
        if len(paid) >= 2:
            base = statistics.median(paid)
        else:
            base = old or list_unit or (paid[0] if paid else None)
        if not base:
            continue

        sell_through = (m_sold + 1) / (m_sold + m_expired + 2)
        demand = min(1.3, max(0.9, 0.5 + sell_through))
        supply = min(1.0, max(0.85, 1.0 - 0.03 * supplied))
        target = base * demand * supply
        start = old or list_unit or target
        unit = start * (1 - ALPHA) + target * ALPHA
        ceiling = 4 * (list_unit or base)
        unit = int(round(min(ceiling, max(unit, item["sell"], 1))))
        learned[entry] = (unit, int(round(list_unit)) if list_unit else None, len(paid) + m_sold + m_expired + supplied)
    return learned


def write_prices(learned):
    if not learned:
        sql(f"DELETE FROM market_price WHERE updated_at < NOW() - INTERVAL {STALE_DAYS} DAY", fetch=False)
        return
    values = ",".join(f"({e},{u},{'NULL' if l is None else l},{s},NOW())" for e, (u, l, s) in sorted(learned.items()))
    sql("INSERT INTO market_price (item_entry, unit_copper, list_copper, samples, updated_at) VALUES " + values +
        " ON DUPLICATE KEY UPDATE unit_copper = VALUES(unit_copper), "
        "list_copper = COALESCE(VALUES(list_copper), list_copper), samples = VALUES(samples), updated_at = NOW(); "
        f"DELETE FROM market_price WHERE updated_at < NOW() - INTERVAL {STALE_DAYS} DAY", fetch=False)


# ---------------------------------------------------------------------------------------------------------------------
# talk

def spoken_name(item):
    name = item["name"]
    return name.lower() if item["cls"] in COMMODITY_CLASSES else name


def with_article(item):
    if item["cls"] in COMMODITY_CLASSES:
        return spoken_name(item)
    return ("an " if item["name"][:1].lower() in "aeiou" else "a ") + item["name"]


def market_words(events, items, rng):
    """[(zone, team, entry, words)] from the last day of each house."""
    day_ago = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 86400))
    out = []
    for house, (team, cities) in MARKETS.items():
        evs = [e for e in events if e["house"] == house and e["ts"] >= day_ago and e["entry"] in items
               and not any(ch.isdigit() for ch in items[e["entry"]]["name"])]
        facts = []

        stock = collections.Counter(e["entry"] for e in evs if e["event"] == "list" and e["seller_kind"] == "merchant"
                                    and items[e["entry"]]["cls"] in COMMODITY_CLASSES)
        for entry, _ in stock.most_common(3):
            sellers = collections.Counter(e["seller"] for e in evs if e["entry"] == entry and e["event"] == "list"
                                          and e["seller_kind"] == "merchant" and e["seller"])
            seller = sellers.most_common(1)[0][0] if sellers else None
            name = spoken_name(items[entry])
            facts.append((entry, lambda city, into, n=name, s=seller,
                          p=rng.randrange(len(GLUT_WORDS)), f=rng.randrange(len(GLUT_FROM)):
                          GLUT_WORDS[p].format(n=n[:1].upper() + n[1:], ln=n, city=city, into=into)
                          + (GLUT_FROM[f].format(s=s) if s else ".")))

        bought = {e["entry"] for e in evs if e["event"] == "sale" and e["buyer_kind"] in ("player", "bot")}
        for entry in rng.sample(sorted(bought), min(2, len(bought))):
            thing = with_article(items[entry])
            facts.append((entry, lambda city, into, t=thing, p=rng.randrange(len(BOUGHT_WORDS)):
                          BOUGHT_WORDS[p].format(t=t, T=t[:1].upper() + t[1:], city=city, into=into)))

        expired = collections.Counter(e["entry"] for e in evs if e["event"] == "expire" and e["seller_kind"] == "merchant"
                                      and items[e["entry"]]["quality"] >= 2)
        unsold = [entry for entry, n in expired.most_common(10) if entry not in bought and n >= 2]
        for entry in unsold[:1]:
            thing = spoken_name(items[entry])
            facts.append((entry, lambda city, into, t=thing, p=rng.randrange(len(UNSOLD_WORDS)):
                          UNSOLD_WORDS[p].format(t=t, T=t[:1].upper() + t[1:], city=city, into=into)))

        townsfolk = [e for e in evs if e["event"] == "list" and e["seller_kind"] == "bot" and e["seller"]]
        for e in rng.sample(townsfolk, min(2, len(townsfolk))):
            thing = with_article(items[e["entry"]])
            facts.append((e["entry"], lambda city, into, s=e["seller"], t=thing,
                          p=rng.randrange(len(SELLING_WORDS)):
                          SELLING_WORDS[p].format(s=s, t=t, T=t[:1].upper() + t[1:], city=city, into=into)))

        rng.shuffle(facts)
        for zone, city, into in cities:
            for entry, words in facts[:WORDS_PER_ZONE]:
                out.append((zone, team, entry, words(city, into)[:255]))
    return out


def write_words(words):
    statements = ["DELETE FROM market_word"]
    if words:
        statements.append("INSERT INTO market_word (zone_id, team, item_entry, words, expires_at) VALUES " + ",".join(
            f"({z},{t},{e},{quote(w)},NOW() + INTERVAL {WORD_HOURS} HOUR)" for z, t, e, w in words))
    sql("; ".join(statements), fetch=False)


# ---------------------------------------------------------------------------------------------------------------------
# dashboard

def dashboard(events, items, stored, learned):
    houses = {h: dict(id=h, name=n, listings=0, merchant=0, bot=0, player=0) for h, n in HOUSES.items()}
    for house, kind, n in sql(f"SELECT ah.houseid, COALESCE({kind_sql('ah.itemowner')}, 'player') AS k, COUNT(*) "
                              "FROM auctionhouse ah GROUP BY ah.houseid, k"):
        if int(house) in houses:
            houses[int(house)][kind] += int(n)
            houses[int(house)]["listings"] += int(n)

    now = time.time()
    hours = []
    for i in range(23, -1, -1):
        label = time.strftime("%Y-%m-%dT%H:00", time.localtime(now - i * 3600))
        hours.append(dict(hour=label, sales=0, copper=0, listed=0, expired=0))
    slot = {h["hour"]: h for h in hours}
    for e in events:
        h = slot.get(e["ts"][:13] + ":00")
        if not h:
            continue
        if e["event"] == "sale":
            h["sales"] += 1
            h["copper"] += e["price"]
        elif e["event"] == "list":
            h["listed"] += 1
        elif e["event"] == "expire":
            h["expired"] += 1

    day_ago = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 86400))
    day = [e for e in events if e["ts"] >= day_ago and e["entry"] in items]
    sales = collections.Counter(e["entry"] for e in day if e["event"] == "sale")
    lists = collections.Counter(e["entry"] for e in day if e["event"] == "list")
    top = sorted(set(sales) | set(lists), key=lambda x: (-sales[x], -lists[x]))[:15]
    prices = {e: v["unit"] for e, v in stored.items()}
    prices.update({e: u for e, (u, _, _) in learned.items()})

    def listed_unit(entry):
        units = [e["buyout"] / e["count"] for e in day if e["entry"] == entry and e["event"] == "list" and e["buyout"]]
        return int(statistics.median(units)) if units else None

    top_goods = [dict(entry=x, name=items[x]["name"], quality=items[x]["quality"], sales=sales[x], listings=lists[x],
                      unit_copper=listed_unit(x), market_copper=prices.get(x)) for x in top]
    moved = sum(1 for e, (u, l, _) in learned.items() if l and abs(u / l - 1) > 0.1)
    recent = [dict(ts=e["ts"], entry=e["entry"], name=items[e["entry"]]["name"], quality=items[e["entry"]]["quality"],
                   count=e["count"], copper=e["price"], seller=e["seller"], seller_kind=e["seller_kind"],
                   buyer_kind=e["buyer_kind"] or "player", house=e["house"])
              for e in reversed(events) if e["event"] == "sale" and e["entry"] in items][:20]
    return {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "houses": list(houses.values()), "hours": hours,
            "top_goods": top_goods, "prices": {"tracked": len(prices), "moved": moved}, "recent": recent}


def write_json(data):
    MARKET_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = MARKET_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(MARKET_JSON)


# ---------------------------------------------------------------------------------------------------------------------

def cycle(dry_run=False, rng=None):
    rng = rng or random.Random()
    if not dry_run:
        sql((HERE / "market_tables.sql").read_text(), fetch=False)
    events = load_events(WINDOW_HOURS)
    items = load_items({e["entry"] for e in events})
    stored = load_prices()
    learned = {} if NO_PRICES.exists() else learn_prices(events, items, stored)
    words = [] if NO_WORDS.exists() else market_words(events, items, rng)
    data = dashboard(events, items, stored, learned)
    log(f"{len(events)} auction events in {WINDOW_HOURS} h; {len(learned)} prices learned; {len(words)} market words; "
        + ", ".join(f"{h['name']} {h['listings']}" for h in data["houses"]))
    if dry_run:
        for z, t, e, w in words[:12]:
            print(f"  zone {z} team {t}: {w}")
        for e, (u, l, s) in sorted(learned.items())[:12]:
            print(f"  {items[e]['name']}: {u} (listing {l}, {s} samples)")
        return
    if NO_PRICES.exists():
        sql("DELETE FROM market_price", fetch=False)
    else:
        write_prices(learned)
    write_words(words)
    write_json(data)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="cycle forever")
    run.add_argument("--interval", type=int, default=600)
    once = sub.add_parser("once", help="one cycle")
    once.add_argument("--dry-run", action="store_true", help="print what would be written, write nothing")
    args = ap.parse_args()

    if args.cmd == "once":
        cycle(dry_run=args.dry_run)
        return
    while True:
        if PAUSE.exists():
            log("paused")
        else:
            try:
                cycle()
            except Exception as exc:     # a bad cycle must not kill the service; the next one tries again
                log(f"cycle failed: {exc}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
