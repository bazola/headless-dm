-- Guild seats and relations (custom wow plans/14, step B1): companies as small fiefdoms.
-- Seeded once from lore_guild by /opt/wow/regard/rivalry.py; incidents (step B2) move stances later.
-- Lives in acore_characters.

CREATE TABLE IF NOT EXISTS guild_seat (
    guildid      INT UNSIGNED NOT NULL,
    band         VARCHAR(16)  NOT NULL COMMENT 'recruits (levels 1-20), blooded (20-40) or seasoned (40-60)',
    zone_id      INT UNSIGNED NOT NULL COMMENT 'AreaTable zone id',
    zone_name    VARCHAR(64)  NOT NULL,
    hold         VARCHAR(255) NOT NULL COMMENT 'What the company keeps there and why, one in-world sentence',
    model        VARCHAR(64)  NOT NULL,
    era          VARCHAR(16)  NOT NULL,
    generated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guildid, band),
    KEY by_zone (zone_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS guild_relation (
    guild_a          INT UNSIGNED NOT NULL COMMENT 'Lower guildid',
    guild_b          INT UNSIGNED NOT NULL COMMENT 'Higher guildid',
    stance           FLOAT        NOT NULL COMMENT '-100 blood feud .. 100 sworn allies',
    disposition      VARCHAR(16)  NOT NULL COMMENT 'Drawn at seeding: enemies, rivals, wary, respect or allies',
    shared_zones     JSON         NULL     COMMENT 'Zone ids where both keep a seat',
    origin           TEXT         NOT NULL COMMENT 'How it began and how things stand',
    moments          JSON         NOT NULL COMMENT 'Past moments between the companies, in-world lines',
    a_says           VARCHAR(160) NOT NULL COMMENT 'What members of guild_a say of guild_b',
    b_says           VARCHAR(160) NOT NULL COMMENT 'What members of guild_b say of guild_a',
    last_incident_at DATETIME     NULL     COMMENT 'Set by incidents (step B2)',
    model            VARCHAR(64)  NOT NULL,
    era              VARCHAR(16)  NOT NULL,
    generated_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_a, guild_b)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Step B2: influence, holdings and incidents, kept by regard.py from mod-ledger's events.

CREATE TABLE IF NOT EXISTS guild_influence (
    guildid    INT UNSIGNED NOT NULL,
    zone_id    INT UNSIGNED NOT NULL COMMENT 'A land from lands.py; deeds in a dungeon count for the land it lies in',
    influence  FLOAT        NOT NULL DEFAULT 0 COMMENT 'Members'' deeds there, decaying by half a week',
    decayed_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guildid, zone_id),
    KEY by_zone (zone_id, influence)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS guild_holding (
    zone_id      INT UNSIGNED NOT NULL PRIMARY KEY,
    holder       INT UNSIGNED NOT NULL COMMENT 'The company that holds the land',
    challenger   INT UNSIGNED NULL     COMMENT 'Set while another company contests it',
    holder_since DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS guild_incident (
    id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    ts           DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    guild_a      INT UNSIGNED NOT NULL COMMENT 'The company that acted',
    guild_b      INT UNSIGNED NULL     COMMENT 'The company it touched',
    zone_id      INT UNSIGNED NULL,
    kind         VARCHAR(16)  NOT NULL COMMENT 'claimed, taken, released, contested, held, duel, same_prey, words',
    detail       VARCHAR(255) NOT NULL COMMENT 'What happened, in-world words for the chronicler and prompts',
    stance_delta FLOAT        NOT NULL DEFAULT 0,
    KEY by_ts (ts),
    KEY by_pair (guild_a, guild_b, kind, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Step B3: the words tier. Rewritten by regard.py every cycle; mod-ollama-chat (local patch) loads them
-- with the regard table and adds them to prompts.

CREATE TABLE IF NOT EXISTS guild_words (
    guildid    INT UNSIGNED  NOT NULL PRIMARY KEY,
    words      VARCHAR(1000) NOT NULL COMMENT 'What members know of their company''s standing, second person',
    updated_at DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS land_words (
    zone_id    INT UNSIGNED NOT NULL PRIMARY KEY,
    words      VARCHAR(255) NOT NULL COMMENT 'Who holds the land, as it is told there',
    updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
