-- custom wow plans/18: the player and the companies. Created by regard.py every cycle (IF NOT EXISTS).

-- Step P2: how an unguilded bot answered a real player's charter or company invite. Written by mod-playerbots
-- (local patch, AiPlayerbot.CompanyRegardGate = 1), which looks for the table at each standing reload; regard.py
-- turns answers into reasons.
CREATE TABLE IF NOT EXISTS company_answer (
    id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    ts          DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    bot_guid    INT UNSIGNED NOT NULL COMMENT 'The bot who answered',
    player_guid INT UNSIGNED NOT NULL COMMENT 'The real player who asked',
    guildid     INT UNSIGNED NULL     COMMENT 'The company the bot was asked into; NULL for a charter',
    kind        VARCHAR(8)   NOT NULL COMMENT 'sign (a charter) or join (a company invite)',
    accepted    TINYINT      NOT NULL,
    reason      VARCHAR(16)  NOT NULL COMMENT 'trusts_them, friend_inside, stranger, hardly_knows, not_enough, dislikes, enemy_inside, in_company, invited, unwilling',
    KEY by_ts (ts),
    KEY by_pair (bot_guid, player_guid, kind, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Step P3: where each real player stands with each company. Rewritten by regard.py every cycle; a company that
-- has not noticed them has no row.
CREATE TABLE IF NOT EXISTS company_candidacy (
    guildid        INT UNSIGNED NOT NULL,
    player_guid    INT UNSIGNED NOT NULL,
    stage          VARCHAR(12)  NOT NULL COMMENT 'noticed, spoken_for, offered, joined',
    sponsor_guid   INT UNSIGNED NULL     COMMENT 'The officer or leader who speaks for them (spoken_for, offered)',
    vouchers       INT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Members who know them well and think well of them',
    standing       FLOAT        NOT NULL DEFAULT 0 COMMENT 'Mean regard of members who know them, weighted by rank',
    blackball_guid INT UNSIGNED NULL     COMMENT 'An officer or the leader set against them',
    since          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'When this stage was reached',
    updated_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guildid, player_guid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Step P4: what a company has decided about a real player (invite, raise, lower, cast out). regard.py writes it;
-- mod-playerbots (local patch, AiPlayerbot.CompanyActions = 1) has an officer bot of the company carry it out through
-- its own session and marks the row done.
CREATE TABLE IF NOT EXISTS company_action (
    id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    guildid     INT UNSIGNED NOT NULL,
    player_guid INT UNSIGNED NOT NULL,
    action      VARCHAR(8)   NOT NULL COMMENT 'invite, promote, demote, remove (an officer acts); leave, whisper (prefer_guid acts, P6)',
    near_only   TINYINT      NOT NULL DEFAULT 1 COMMENT 'Only an officer near the player may carry it out',
    prefer_guid INT UNSIGNED NULL     COMMENT 'The member who carries it out if able (the sponsor of an invite)',
    words       VARCHAR(255) NOT NULL DEFAULT '' COMMENT 'What the officer whispers to the player as it is done',
    actor_guid  INT UNSIGNED NULL     COMMENT 'Who carried it out',
    done_at     DATETIME     NULL,
    result      VARCHAR(12)  NULL     COMMENT 'sent, moot, gone (mod-playerbots); expired, withdrawn, declined (regard.py)',
    KEY pending (done_at, guildid, player_guid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Step P8c: conversations between bots with no real player about, written by a writing lane and judged like chat.
-- Nothing of it is said in the game; the feelings it moves are real. Shown on the dashboard ("Overheard").
CREATE TABLE IF NOT EXISTS bot_talk (
    id       BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    ts       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    map_id   INT UNSIGNED NOT NULL,
    zone_id  INT UNSIGNED NOT NULL,
    place    VARCHAR(64)  NOT NULL COMMENT 'Where, as words: "in Westfall"',
    a_guid   INT UNSIGNED NOT NULL,
    b_guid   INT UNSIGNED NOT NULL,
    exchange JSON         NOT NULL COMMENT '[[speaker name, line], ...]',
    moments  JSON         NOT NULL COMMENT '[{speaker, target, feels, why}] as the judge read them',
    model    VARCHAR(64)  NOT NULL,
    KEY by_ts (ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Step P8d-f: every change bot companies decide on for their people, for the daily caps and the record. A move's
-- invite is made once its bot has left its old company (invited).
CREATE TABLE IF NOT EXISTS company_churn (
    id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    ts           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    kind         VARCHAR(8)   NOT NULL COMMENT 'recruit, loyalty, move',
    bot_guid     INT UNSIGNED NOT NULL,
    from_guild   INT UNSIGNED NULL,
    to_guild     INT UNSIGNED NULL,
    officer_guid INT UNSIGNED NULL     COMMENT 'Who asks (recruit, move)',
    invited      TINYINT      NOT NULL DEFAULT 0 COMMENT 'move: the new company''s invite has been made',
    KEY by_kind (kind, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Step P6: loyalty. How long each bot in a company led by a real player has thought badly of its leader.
CREATE TABLE IF NOT EXISTS company_loyalty (
    guildid    INT UNSIGNED NOT NULL,
    bot_guid   INT UNSIGNED NOT NULL,
    sour_since DATETIME     NOT NULL COMMENT 'Since when its regard for the leader has been at or below the loyalty line',
    checked_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guildid, bot_guid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Step P4: the rank ladder's memory between hourly checks.
CREATE TABLE IF NOT EXISTS company_rank (
    guildid       INT UNSIGNED     NOT NULL,
    player_guid   INT UNSIGNED     NOT NULL,
    seen_rank     TINYINT UNSIGNED NOT NULL COMMENT 'guild_member.rank at the check: 0 leader .. 4 newest',
    failing_since DATETIME         NULL     COMMENT 'Since when the rank''s needs, less the slack, have not been met',
    checked_at    DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guildid, player_guid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Step P4: influence a real player's deeds brought their company (never decays; the ladder's veteran and officer
-- needs).
CREATE TABLE IF NOT EXISTS company_deeds (
    guildid     INT UNSIGNED NOT NULL,
    player_guid INT UNSIGNED NOT NULL,
    influence   FLOAT        NOT NULL DEFAULT 0,
    updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guildid, player_guid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Step P4: in-world names for a company's five standings, written by rivalry.py ranks and projected to guild_rank
-- (shown after a worldserver restart: guilds load at startup).
CREATE TABLE IF NOT EXISTS company_rank_name (
    guildid      INT UNSIGNED     NOT NULL,
    rid          TINYINT UNSIGNED NOT NULL COMMENT '0 who leads .. 4 newest recruit',
    rname        VARCHAR(20)      NOT NULL,
    model        VARCHAR(64)      NOT NULL,
    era          VARCHAR(16)      NOT NULL,
    generated_at DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guildid, rid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Step P5: a company raised by a real player's charter getting its story. regard.py notices it and starts
-- founding.py, which works through the states and records its progress here. Keyed like the registry: a reused id
-- with another createdate is another company.
CREATE TABLE IF NOT EXISTS company_founding (
    guildid       INT UNSIGNED NOT NULL PRIMARY KEY,
    founded_at    INT UNSIGNED NOT NULL COMMENT 'guild.createdate',
    founder_guid  INT UNSIGNED NOT NULL,
    state         VARCHAR(12)  NOT NULL DEFAULT 'pending' COMMENT 'The last step finished: pending, lore, ranks, relations, done',
    attempts      INT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Runs so far; at 3 an unfinished founding is given up (founding.py --force)',
    last_attempt  DATETIME     NULL,
    heir_ended_id INT UNSIGNED NULL     COMMENT 'company_ended row it is heir to, when most founders rode with it',
    zone_id       INT UNSIGNED NULL     COMMENT 'Where the charter was turned in (ledger guild_found)',
    detail        VARCHAR(255) NULL     COMMENT 'Last error, or what was written',
    updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Step P4b: company lifecycle. A company is (guildid, founded_at): the core hands out MAX(guildId) + 1 at startup, so
-- a dead company's id can be reused. regard.py refreshes the registry every cycle; a registry row whose guild is gone
-- or has another createdate is a dead company, whose rows are moved to company_archive before anything else runs.
CREATE TABLE IF NOT EXISTS company_registry (
    guildid     INT UNSIGNED NOT NULL PRIMARY KEY,
    founded_at  INT UNSIGNED NOT NULL COMMENT 'guild.createdate (unix time)',
    name        VARCHAR(24)  NOT NULL,
    leader_guid INT UNSIGNED NOT NULL,
    members     JSON         NOT NULL COMMENT '[[guid, rank], ...] as last seen',
    first_seen  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    seen_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS company_ended (
    ended_id    INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    guildid     INT UNSIGNED NOT NULL COMMENT 'The id it had; may belong to another company now',
    founded_at  INT UNSIGNED NULL,
    name        VARCHAR(24)  NOT NULL,
    leader_guid INT UNSIGNED NULL,
    members     JSON         NOT NULL COMMENT '[[guid, rank, name], ...] as last seen',
    ended_at    DATETIME     NOT NULL COMMENT 'The ledger guild_disband row, or when the loss was noticed',
    noticed_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    cause       VARCHAR(16)  NOT NULL COMMENT 'disbanded (ledger row seen), vanished, orphaned (rows with no company)',
    KEY by_guild (guildid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS company_archive (
    id       BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    ended_id INT UNSIGNED NOT NULL,
    source   VARCHAR(32)  NOT NULL COMMENT 'The table the row came from',
    data     JSON         NOT NULL COMMENT 'The whole row',
    KEY by_ended (ended_id, source)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Lines added to a character's story after it was written (e.g. the company they rode with broke apart).
-- gen_backstories.py project appends them to the BIO_ / BIOX_ templates.
CREATE TABLE IF NOT EXISTS lore_character_note (
    id         BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    guid       INT UNSIGNED NOT NULL,
    source     VARCHAR(16)  NOT NULL COMMENT 'lifecycle',
    note       VARCHAR(255) NOT NULL COMMENT 'Second person, in-world, no numbers',
    created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY by_guid (guid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Step P3: a clause a bot carries about someone beside its regard sentence, e.g. a sponsor's mind to bring a player
-- into the company. mod-ollama-chat (local patch) appends it to that person's regard line. Each source rewrites its
-- own rows every cycle.
CREATE TABLE IF NOT EXISTS regard_aside (
    bot_guid   INT UNSIGNED NOT NULL,
    other_guid INT UNSIGNED NOT NULL,
    source     VARCHAR(16)  NOT NULL COMMENT 'The step that owns the row: candidacy',
    words      VARCHAR(255) NOT NULL COMMENT 'Second person, in-world, no numbers',
    updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (bot_guid, other_guid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
