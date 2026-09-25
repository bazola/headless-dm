#!/usr/bin/env python3
"""Era settings for lore generation (custom wow plans/12-PLAN-release-schedule.md).

An era is "now" for everyone in the world. Lore written for an era must not
treat anything later as having happened. The world moves forward (classic ->
tbc -> wotlk); when it does, characters gain a new chapter rather than being
rewritten. tbc is not defined yet: write it before the Dark Portal opens.
"""
import re

# Out-of-world vocabulary, for every era. Plain English words that also have a
# game sense ("raid", "instance", "mob", "loot", "healer", "realm") are left out:
# an orc raid, a village healer or the realm of Stormwind are in-world.
BASE_META = (r"level(ed|s)?\s*\d+|lvl|xp|experience points|dps|spec|talent tree|"
             r"raid (boss|lockout|group)|dungeon finder|respawn|npc|players?|server|"
             r"patch|expansion|addon|guild bank|achievement|quest log|pvp|pve|world of warcraft|"
             r"burning crusade|wrath of the lich king|cataclysm|vanilla|"
             # After the war against the Lich King. (Kul Tiras and the Zandalar tribe are older.)
             r"broken isles|suramar|legion invasion|deathwing|pandaria|"
             r"azerite|shadowlands|warchief garrosh|warchief sylvanas|warchief vol'jin|"
             r"garrosh hellscream's horde|garrosh's madness|siege of orgrimmar|void elf|nightborne|highmountain")

CLASSIC_SETTING = (
    "Azeroth in the uneasy years after the Third War. The orcs came through the Dark Portal a generation "
    "ago; it now stands dead and cold in the Blasted Lands, and no one has crossed it in years. The Burning "
    "Legion was thrown back at Mount Hyjal, at the cost of the World Tree and the night elves' immortality. "
    "Lordaeron has fallen: Prince Arthas betrayed his people and went north to his master the Lich King, the "
    "Scourge holds the Plaguelands, Dalaran lies in ruins with Archmage Antonidas slain, and the Forsaken "
    "under Sylvanas have made the ruins beneath Lordaeron their Undercity. Thrall leads the new Horde from "
    "Orgrimmar in Durotar with the Darkspear trolls, Cairne Bloodhoof's tauren and, uneasily, the Forsaken. "
    "In Stormwind King Varian Wrynn has vanished and Highlord Bolvar Fordragon rules as regent; Jaina "
    "Proudmoore holds Theramore; King Magni's dwarves, the gnomes driven from Gnomeregan and the night elves "
    "of Darnassus stand with the Alliance. Dark Iron dwarves and the black dragonflight fester in Blackrock "
    "Mountain, the Argent Dawn and the Scarlet Crusade each fight the Scourge in their own way, and the "
    "trolls of Zul'Gurub and the silithid beyond the Scarab Wall stir in the south. Nothing later has "
    "happened: no army has sailed for Northrend, no one has marched into Outland, the high elves are a "
    "scattered remnant, and no blood elves or draenei live among the Horde or the Alliance.")

WOTLK_SETTING = (
    "Azeroth during the war against the Lich King (after the Third War, the return of the Burning Legion "
    "through the Dark Portal, and the opening of the Northrend campaign).")

ERAS = {
    "classic": dict(
        setting=CLASSIC_SETTING,
        limits="",
        guild_now="what it is working toward now",
        max_level=60,
        standing={60: "among the most seasoned fighters of the age, tested against the Scourge and the "
                      "Blackrock clans"},
        races={1, 2, 3, 4, 5, 6, 7, 8},
        classes={1, 2, 3, 4, 5, 7, 8, 9, 11},
        meta_extra=(r"northrend|icecrown|wrathgate|angrathar|borean tundra|warsong hold|howling fjord|dragonblight|"
                    r"grizzly hills|zul'drak|sholazar|storm peaks|crystalsong|wintergrasp|ulduar|"
                    r"argent crusade|ebon blade|ebon hold|acherus|"
                    r"outland|hellfire peninsula|hellfire citadel|shattrath|zangarmarsh|terokkar|netherstorm|"
                    r"shadowmoon valley|blade's edge (?:mountains|arena|peaks)|naaru|draenei|exodar|azuremyst|bloodmyst|"
                    r"blood ?elf|blood ?elves|sin'dorei|lor'themar|garrosh|highlord tirion|"
                    # Places first named in later ages (the Scarlet Enclave is the death knights' Acherus).
                    # Silvermoon belongs with eversong and the ghostlands: high elf country, and the models put
                    # night elves there. The operator hand-fixed "the Silvermoon groves" once already
                    # (handoff 20 §3), and a run on 2026-09-16 bore a night elf "in the shadow of the
                    # Silvermoon ruins". Ruined Quel'Thalas itself stays legitimate; only the city is banned.
                    r"silvermoon|"
                    r"scarlet enclave|dead scar|eversong|ghostlands|tranquillien|"
                    # Found drafting the capitals' almanac, 2026-09-24: the Spire stands in Eversong (a seedless
                    # Thunder Bluff put it on the mesa), and the Harbor and the Ring came with the Lich King's war.
                    r"windrunner spire|stormwind harbor|ring of valor|"
                    # Darnassus and Teldrassil stand in this age. Their burning is the War of the Thorns, ages
                    # later; a run on 2026-09-16 had a night elf watch "the fires of Darnassus burn" and both the
                    # regex and the judge let it through. The city's name alone is legitimate, only its burning.
                    r"war of the thorns|(?:darnassus|teldrassil)[^.]{0,30}burn|burn\w*[^.]{0,30}(?:darnassus|teldrassil)|"
                    r"beyond the dark portal|dark portal.{0,40}reopen\w*|reopen\w*.{0,40}dark portal|"
                    r"floating city|fall of the lich king|lich king's (fall|defeat)"),
        judge_allowed=(
            "the Scourge and the Plaguelands; Arthas as the traitor prince who went north to serve the Lich "
            "King; the Third War and the fall of Lordaeron; ruined Dalaran; the orcs coming through the Dark "
            "Portal long ago and the dead Portal in the Blasted Lands; high elves and the ruins of Quel'Thalas; "
            "the Argent Dawn and the Scarlet Crusade; Naxxramas over the Plaguelands; Blackrock Mountain, "
            "Ragnaros and the black dragonflight; Onyxia; Ahn'Qiraj and the silithid; Zul'Gurub and Hakkar; "
            "Kul Tiras; the Zandalar tribe; Illidan's ancient betrayal; Bolvar Fordragon as regent; Varian "
            "Wrynn missing; Thrall as Warchief; Sylvanas ruling the Forsaken; Dalaran in ruins and whatever survives "
            "of its archives; Archmage Antonidas remembered as slain; the Horde and the Alliance; any company, "
            "mercenary band, person or place invented for a story."),
        judge_forbidden=(
            "any campaign, battle or journey in Northrend (Icecrown, the Wrathgate, the Borean Tundra, the "
            "Howling Fjord, Dragonblight and the rest); the Argent Crusade or the Knights of the Ebon Blade; "
            "death knights serving the Horde or the Alliance; Outland, Shattrath, Hellfire Peninsula, or anyone "
            "crossing the Dark Portal in recent years; the naaru; draenei or blood elves living among the "
            "Alliance or the Horde; Silvermoon rebuilt; Dalaran floating in the sky; Varian Wrynn back on his "
            "throne; Garrosh Hellscream; Archmage Antonidas alive; the Lich King defeated; Deathwing's return "
            "and anything later."),
    ),
    "wotlk": dict(
        setting=WOTLK_SETTING,
        limits=(" Nothing after this war has happened yet: no Deathwing, no Cataclysm, no Siege of Orgrimmar, "
                "no Pandaria, no Broken Isles. Thrall is Warchief; Garrosh is only an overlord in Northrend."),
        guild_now="where it stands in the war now",
        max_level=80,
        standing={60: "a veteran of the campaigns beyond the Dark Portal",
                  70: "a hardened veteran of the war in Northrend"},
        races={1, 2, 3, 4, 5, 6, 7, 8, 10, 11},
        classes={1, 2, 3, 4, 5, 6, 7, 8, 9, 11},
        meta_extra="",
        judge_allowed="everything up to and including the war against the Lich King in Northrend.",
        judge_forbidden=("Deathwing's return, the Cataclysm, Garrosh as Warchief, the Siege of Orgrimmar, "
                         "Pandaria, the Broken Isles and anything later."),
    ),
}


def get(name):
    if name not in ERAS:
        raise SystemExit(f"unknown era {name!r}; defined: {', '.join(ERAS)}")
    e = dict(ERAS[name], name=name)
    pattern = BASE_META + ("|" + e["meta_extra"] if e["meta_extra"] else "")
    e["meta"] = re.compile(r"\b(" + pattern + r")\b", re.I)
    e["targets"] = MOTIVATION_TARGETS.get(name, [])
    return e


def standing(level, e):
    level = min(level, e["max_level"])
    if level < 10:
        return "barely trained and new to the road"
    if level < 30:
        return "blooded in a few hard fights"
    if level < 60:
        return "seasoned and well travelled"
    if level < 70 or 70 not in e["standing"]:
        return e["standing"][60]
    return e["standing"][70]


# ---------------------------------------------------------------------------
# Motivations (plan 15)

MOTIVATION_KINDS = ["vengeance", "duty", "faith", "redemption", "protection", "wealth", "knowledge", "glory",
                    "love", "freedom", "belonging", "survival"]

# Leanings, not rules: each listed kind gets extra weight in the draw of three.
RACE_KIND_BIAS = {
    1: ("duty", "protection", "love"), 2: ("glory", "belonging", "freedom"), 3: ("knowledge", "glory", "wealth"),
    4: ("protection", "faith", "duty"), 5: ("vengeance", "freedom", "redemption"), 6: ("protection", "faith", "belonging"),
    7: ("knowledge", "belonging", "wealth"), 8: ("survival", "belonging", "faith"),
    10: ("redemption", "knowledge", "glory"), 11: ("faith", "redemption", "protection"),
}
CLASS_KIND_BIAS = {
    1: ("glory", "duty", "protection"), 2: ("faith", "duty", "redemption"), 3: ("protection", "freedom", "survival"),
    4: ("wealth", "freedom", "vengeance"), 5: ("faith", "redemption", "love"), 6: ("redemption", "vengeance", "duty"),
    7: ("faith", "belonging", "duty"), 8: ("knowledge", "glory", "protection"), 9: ("knowledge", "freedom", "vengeance"),
    11: ("protection", "faith", "belonging"),
}
# Words in a backstory that make a kind more likely.
KIND_CUES = {
    "vengeance": ("aveng", "murder", "slaughter", "killed your", "revenge"),
    "wealth": ("coin", "gold", "debt", "merchant", "trade"),
    "faith": ("the light", "elune", "earth mother", "spirits", "loa", "prayer"),
    "love": ("beloved", "wife", "husband", "sweetheart", "betrothed"),
    "redemption": ("shame", "guilt", "failed", "betray"),
    "knowledge": ("book", "study", "scholar", "secret", "arcane"),
    "protection": ("protect", "defend", "guard", "shield"),
    "belonging": ("outcast", "exile", "orphan", "alone", "homeless"),
}


def draw_kinds(race, cls, backstory, rng, n=3):
    """Offer n distinct motivation kinds, weighted by race, class and cues in the backstory."""
    weights = {k: 1.0 for k in MOTIVATION_KINDS}
    for k in RACE_KIND_BIAS.get(race, ()):
        weights[k] += 1.5
    for k in CLASS_KIND_BIAS.get(cls, ()):
        weights[k] += 1.5
    text = backstory.lower()
    for k, cues in KIND_CUES.items():
        if any(c in text for c in cues):
            weights[k] += 1.0
    picks = []
    for _ in range(n):
        roll = rng.uniform(0, sum(weights.values()))
        for k, w in weights.items():
            roll -= w
            if roll <= 0:
                break
        picks.append(k)
        del weights[k]
    return picks


# (name, type, where it is found). Where feeds the behaviour tier later: bots drift toward their target.
MOTIVATION_TARGETS = {
    "classic": [
        ("Ragnaros", "foe", "Blackrock Mountain"), ("Onyxia", "foe", "Dustwallow Marsh"),
        ("Nefarian", "foe", "Blackrock Spire"), ("General Drakkisath", "foe", "Blackrock Spire"),
        ("Warchief Rend Blackhand", "foe", "Blackrock Spire"), ("Emperor Dagran Thaurissan", "foe", "Blackrock Depths"),
        ("Hakkar", "foe", "Stranglethorn Vale"), ("C'Thun", "foe", "Silithus"), ("Kel'Thuzad", "foe", "Eastern Plaguelands"),
        ("Baron Rivendare", "foe", "Stratholme"), ("Grand Crusader Dathrohan", "foe", "Stratholme"),
        ("Darkmaster Gandling", "foe", "Western Plaguelands"), ("High Inquisitor Whitemane", "foe", "Tirisfal Glades"),
        ("Edwin VanCleef", "foe", "Westfall"), ("Archmage Arugal", "foe", "Silverpine Forest"),
        ("Lord Kazzak", "foe", "Blasted Lands"), ("Azuregos", "foe", "Azshara"),
        ("Mutanus the Devourer", "foe", "The Barrens"), ("Princess Theradras", "foe", "Desolace"),
        ("the Scourge", "faction", "Plaguelands"), ("the Scarlet Crusade", "faction", "Tirisfal Glades"),
        ("the Defias Brotherhood", "faction", "Westfall"), ("the Syndicate", "faction", "Alterac Mountains"),
        ("the Dark Iron dwarves", "faction", "Searing Gorge"), ("the Black Dragonflight", "faction", "Burning Steppes"),
        ("the Burning Legion", "faction", "Felwood"), ("the Shadow Council", "faction", "Felwood"),
        ("the Twilight's Hammer", "faction", "Silithus"), ("the Qiraji", "faction", "Silithus"),
        ("the Venture Company", "faction", "Stranglethorn Vale"), ("the Bloodsail Buccaneers", "faction", "Stranglethorn Vale"),
        ("the Horde", "faction", ""), ("the Alliance", "faction", ""),
        ("the Argent Dawn", "cause", "Eastern Plaguelands"), ("the Cenarion Circle", "cause", "Moonglade"),
        ("Gnomeregan", "place", "Dun Morogh"), ("Lordaeron", "place", "Tirisfal Glades"),
        ("Quel'Thalas", "place", "Eastern Plaguelands"),
    ],
    "wotlk": [
        ("the Lich King", "foe", "Icecrown"), ("Kel'Thuzad", "foe", "Dragonblight"), ("Malygos", "foe", "Borean Tundra"),
        ("Yogg-Saron", "foe", "The Storm Peaks"), ("the Scourge", "faction", "Icecrown"),
        ("the Argent Crusade", "cause", "Icecrown"),
    ],
}


def target_known(e, name, story=""):
    """True when a foe/faction target is in the era's list (ignoring a leading 'the') or named in the story."""
    norm = lambda s: re.sub(r"^the\s+", "", s.strip().lower())
    n = norm(name)
    if not n:
        return False
    if any(norm(t) == n or norm(t) in n or n in norm(t) for t, _, _ in e["targets"]):
        return True
    return n in story.lower()


def judge_prompt(e, text, known=()):
    """known: names of people, companies and creatures living now (the small judge flags invented companies
    without them, allowed list or not)."""
    names = ", ".join(sorted({n for n in known if n}))
    return ("You check writing for a world with a fixed present. The present is: " + e["setting"] + "\n\n"
            "These belong to the present or the past and are fine: " + e["judge_allowed"] + "\n\n"
            + ("These are people, companies and creatures of the present, and naming them is fine: " + names + "\n\n"
               if names else "")
            + "These have NOT happened yet; writing that treats them as real or past is wrong: "
            + e["judge_forbidden"] + "\n\n"
            'Text:\n"""\n' + text + '\n"""\n\n'
            "Does the text treat anything that has not happened yet as real, or give a person, people or place "
            "a state they only reach later? Mentions of things that are fine do not count. Reply with JSON only: "
            '{"out_of_time": true or false, "evidence": "the offending words, or an empty string"}')
