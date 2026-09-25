-- The story of a journey (custom wow plans/53): a stretch of one character's record, written up as
-- prose by the chronicler's scribes from what mod-ledger and regard.py already recorded.
--
-- A journey is not a watch. The chronicle divides the day into four-hour watches and writes up the
-- whole realm; this divides ONE character's record into the stretches they actually lived -- from the
-- moment they set out until the company broke up or they stopped doing anything -- and writes up that.
--
-- Written by /opt/wow/chronicler/journey_story.py, driven from the dashboard through the lore gate.
-- Read by the dashboard through dashboard-data/journey-stories/<guid>.json. Lives in acore_characters.

CREATE TABLE IF NOT EXISTS journey_story (
    id         BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    guid       INT UNSIGNED NOT NULL COMMENT 'Whose journey it is',
    main_guid  INT UNSIGNED NOT NULL COMMENT 'The household it belongs to (player_main.main_guid), 0 if none',
    leg_start  DATETIME     NOT NULL COMMENT 'When they set out',
    leg_end    DATETIME     NOT NULL COMMENT 'When the company broke up, or the record went quiet',
    ended      VARCHAR(16)  NOT NULL COMMENT 'How it ended: disband, left, kicked, quiet',
    title      VARCHAR(160) NOT NULL,
    body       TEXT         NOT NULL COMMENT 'The whole story, chapters joined by a blank line',
    chapters   JSON         NOT NULL COMMENT 'One entry per bout: place, from, to, heading, prose',
    facts      JSON         NOT NULL COMMENT 'The deeds the scribe was given, so the story can be checked against them',
    companions JSON         NOT NULL COMMENT 'Guids of everyone who travelled with them in this stretch',
    place      VARCHAR(96)  NOT NULL COMMENT 'Where most of it happened, for the list',
    deeds      INT UNSIGNED NOT NULL DEFAULT 0,
    words      INT UNSIGNED NOT NULL DEFAULT 0,
    model      VARCHAR(64)  NOT NULL,
    era        VARCHAR(16)  NOT NULL,
    flag       VARCHAR(255) NULL COMMENT 'Judge evidence, when the last attempt was still flagged (kept for review)',
    -- A regeneration replaces the story in place and counts up, so "write it again" is not a second row
    -- saying the same things in other words (the trap recall.py paid for: two descending sets of six).
    version    INT UNSIGNED NOT NULL DEFAULT 1,
    created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY by_leg (guid, leg_start),
    KEY by_household (main_guid, leg_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
