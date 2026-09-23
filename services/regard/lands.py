"""The lands companies hold (custom wow plans/14): data only, shared by rivalry.py (seats) and regard.py
(influence and holdings)."""

# (zone id, name, lowest level, highest level, lands). Ids checked against AreaTable.dbc 2026-09-14.
# A = Alliance lands, H = Horde lands, C = contested: both keep footholds there.
# Capitals, Moonglade (a sanctuary) and Deadwind Pass (nothing to hold) are never seats.
CLASSIC_ZONES = [
    (1, "Dun Morogh", 1, 10, "A"), (12, "Elwynn Forest", 1, 10, "A"), (141, "Teldrassil", 1, 10, "A"),
    (14, "Durotar", 1, 10, "H"), (215, "Mulgore", 1, 10, "H"), (85, "Tirisfal Glades", 1, 10, "H"),
    (40, "Westfall", 10, 20, "A"), (38, "Loch Modan", 10, 20, "A"), (148, "Darkshore", 10, 20, "A"),
    (130, "Silverpine Forest", 10, 20, "H"), (17, "The Barrens", 10, 25, "H"),
    (44, "Redridge Mountains", 15, 25, "A"), (406, "Stonetalon Mountains", 15, 27, "C"),
    (331, "Ashenvale", 18, 30, "C"), (10, "Duskwood", 18, 30, "A"), (11, "Wetlands", 20, 30, "A"),
    (267, "Hillsbrad Foothills", 20, 30, "C"), (400, "Thousand Needles", 25, 35, "H"),
    (36, "Alterac Mountains", 30, 40, "C"), (45, "Arathi Highlands", 30, 40, "C"), (405, "Desolace", 30, 40, "C"),
    (33, "Stranglethorn Vale", 30, 45, "C"), (15, "Dustwallow Marsh", 35, 45, "C"), (3, "Badlands", 35, 45, "C"),
    (8, "Swamp of Sorrows", 35, 45, "C"), (357, "Feralas", 40, 50, "C"), (47, "The Hinterlands", 40, 50, "C"),
    (440, "Tanaris", 40, 50, "C"), (51, "Searing Gorge", 43, 50, "C"), (16, "Azshara", 45, 55, "C"),
    (4, "Blasted Lands", 45, 55, "C"), (490, "Un'Goro Crater", 48, 55, "C"), (361, "Felwood", 48, 55, "C"),
    (46, "Burning Steppes", 50, 58, "C"), (28, "Western Plaguelands", 51, 58, "C"),
    (139, "Eastern Plaguelands", 53, 60, "C"), (618, "Winterspring", 53, 60, "C"), (1377, "Silithus", 55, 60, "C"),
]
ZONES = {"classic": CLASSIC_ZONES}

# Instance map id -> the land whose deeds it counts toward. Map ids checked against Map.dbc 2026-09-14.
# Blackrock Mountain: the Depths count for Searing Gorge, the Spire and the raids above for Burning Steppes.
INSTANCE_LANDS = {
    36: 40, 34: 12, 43: 17, 47: 17, 129: 17, 33: 130, 48: 331, 90: 1, 189: 85, 70: 3, 209: 440, 349: 405,
    109: 8, 230: 51, 229: 46, 429: 357, 289: 28, 329: 139, 389: 14, 409: 46, 249: 15, 469: 46, 309: 33,
    509: 1377, 531: 1377, 533: 139,
}

# Instance map id -> what the place is called, with its article, so a deed done inside one can be named.
# A dungeon still counts toward the land above it (INSTANCE_LANDS): this is only how the scribe says where.
# Names taken from Map.dbc 2026-09-20; articles follow LANDS_WITH_THE's habit ("in the Deadmines").
INSTANCE_NAMES = {
    36: "the Deadmines", 34: "the Stormwind Stockade", 43: "the Wailing Caverns", 47: "Razorfen Kraul",
    129: "Razorfen Downs", 33: "Shadowfang Keep", 48: "Blackfathom Deeps", 90: "Gnomeregan",
    189: "the Scarlet Monastery", 70: "Uldaman", 209: "Zul'Farrak", 349: "Maraudon",
    109: "the Sunken Temple", 230: "Blackrock Depths", 229: "Blackrock Spire", 429: "Dire Maul",
    289: "Scholomance", 329: "Stratholme", 389: "Ragefire Chasm", 409: "the Molten Core",
    249: "Onyxia's Lair", 469: "Blackwing Lair", 309: "Zul'Gurub", 509: "the Ruins of Ahn'Qiraj",
    531: "the Temple of Ahn'Qiraj", 533: "Naxxramas",
}

# What stands in each land, so the model doesn't carry a place into the wrong one (the first sample put
# Ratchet in Durotar). Footholds are marked where both factions have one.
LAND_NOTES = {
    1: "Kharanos, Anvilmar, Brewnall Village, the fallen gates of Gnomeregan",
    12: "Goldshire, Northshire Abbey, the Eastvale logging camp, the Tower of Azora",
    141: "Dolanaar, Shadowglen, the Oracle Glade, Rut'theran Village",
    14: "Razor Hill, Sen'jin Village, the Valley of Trials, the Echo Isles",
    215: "Bloodhoof Village, Camp Narache, the Venture Company's mine",
    85: "Brill, Deathknell, the walls of the Scarlet Monastery, the ruins of Lordaeron",
    40: "Sentinel Hill, Moonbrook and the Deadmines beneath it, Jangolode Mine, the Saldean farm",
    38: "Thelsamar, the Stonewrought Dam, Algaz Station, the Ironband excavation",
    148: "Auberdine, the ruins of Ameth'Aran, Twilight Vale, Blackwood Den",
    130: "the Sepulcher, Shadowfang Keep, Pyrewood Village, Ambermill",
    17: "the Crossroads, Camp Taurajo, the goblin port of Ratchet, the Wailing Caverns, Northwatch Hold (Alliance)",
    44: "Lakeshire, Stonewatch Keep, Render's Camp",
    406: "Sun Rock Retreat (Horde), Stonetalon Peak (Alliance), the Venture Company's Windshear Crag",
    331: "Astranaar (Alliance), Maestra's Post (Alliance), Splintertree Post (Horde), Zoram'gar Outpost (Horde), "
         "the Warsong lumber camp",
    10: "Darkshire, the Raven Hill cemetery, the Twilight Grove",
    11: "Menethil Harbor, Dun Modr, the Thandol Span, Grim Batol",
    267: "Southshore (Alliance), Tarren Mill (Horde), Durnholde Keep, the Hillsbrad fields",
    400: "Freewind Post (Horde), the Shimmering Flats, Darkcloud Pinnacle",
    36: "the ruins of Alterac City, Strahnbrad, the shielded ruins of Dalaran on the border",
    45: "Refuge Pointe (Alliance), Hammerfall (Horde), Stromgarde Keep, the Circle of East Binding",
    405: "Nijel's Point (Alliance), Shadowprey Village (Horde), Maraudon, the centaur clans' camps",
    33: "the goblin port of Booty Bay, Grom'gol Base Camp (Horde), the Rebel Camp (Alliance), "
        "Nesingwary's Expedition, the Gurubashi Arena, Zul'Gurub",
    15: "Theramore Isle (Alliance), Brackenwall Village (Horde), Onyxia's Lair",
    3: "Kargath (Horde), Uldaman, Angor Fortress",
    8: "Stonard (Horde), the Sunken Temple, the Misty Reed Strand",
    357: "Feathermoon Stronghold (Alliance), Camp Mojache (Horde), Dire Maul",
    47: "Aerie Peak of the Wildhammer (Alliance), Revantusk Village (Horde), Jintha'Alor, Quel'Danil Lodge",
    440: "the goblin town of Gadgetzan, Steamwheedle Port, Zul'Farrak",
    51: "Thorium Point of the Thorium Brotherhood, the Cauldron, the gates of Blackrock Mountain",
    16: "Talrendis Point (Alliance), Valormok (Horde), the ruins of Eldarath",
    4: "Nethergarde Keep (Alliance), the dead Dark Portal, Dreadmaul Hold, the Tainted Scar",
    490: "Marshal's Refuge, Fire Plume Ridge, the crystal pylons",
    361: "Talonbranch Glade (Alliance), Bloodvenom Post (Horde), the Emerald Sanctuary, Jaedenar, Irontree Woods",
    46: "Morgan's Vigil (Alliance), Flame Crest (Horde), Blackrock Stronghold, Dreadmaul Rock",
    28: "Chillwind Camp (Alliance), the Bulwark (Horde), Andorhal, Caer Darrow and Scholomance, Hearthglen of the "
        "Scarlet Crusade",
    139: "Light's Hope Chapel of the Argent Dawn, Stratholme, Tyr's Hand of the Scarlet Crusade, Corin's Crossing",
    618: "the goblin town of Everlook, Timbermaw Hold, Frostsaber Rock, Mazthoril",
    1377: "Cenarion Hold, the Scarab Wall, the Twilight's Hammer camps, the silithid hives",
}
