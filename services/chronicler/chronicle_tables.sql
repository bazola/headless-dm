-- Chronicle (custom wow plans/19): each faction's scribe writes up every watch of the day from what mod-ledger
-- and the regard service recorded, and the watch's news becomes rumours bots pass on in the lands.
-- Written by /opt/wow/chronicler/chronicler.py. Read by mod-ollama-chat (local patch, rumours) and by the
-- dashboard through chronicle.json. Lives in acore_characters.

CREATE TABLE IF NOT EXISTS chronicle_entry (
    id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    window_start DATETIME     NOT NULL COMMENT 'Start of the watch, local time',
    window_end   DATETIME     NOT NULL,
    faction      CHAR(1)      NOT NULL COMMENT 'A or H: whose scribe wrote it',
    scribe       VARCHAR(64)  NOT NULL,
    title        VARCHAR(160) NOT NULL,
    body         TEXT         NOT NULL COMMENT 'Empty for a quiet watch: nothing worth a line was reported',
    facts        JSON         NOT NULL COMMENT 'The reports the scribe wrote from',
    model        VARCHAR(64)  NOT NULL,
    era          VARCHAR(16)  NOT NULL,
    flag         VARCHAR(255) NULL COMMENT 'Judge evidence, when the last attempt was still flagged (kept for review)',
    created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY by_watch (window_start, faction)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- One telling of a story (round two): told plainly where it happened (hop 0), then retold a little more garbled
-- at each stop along the road, one hop per watch. Word of the other side starts at hop 1, as hearsay.
CREATE TABLE IF NOT EXISTS chronicle_tale (
    id        BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    entry_id  BIGINT UNSIGNED  NOT NULL COMMENT 'The watch whose news it is',
    root_id   BIGINT UNSIGNED  NULL COMMENT 'The first telling of the story; NULL on the first telling itself',
    parent_id BIGINT UNSIGNED  NULL COMMENT 'The telling this one was retold from',
    team      TINYINT UNSIGNED NOT NULL COMMENT 'Who tells it: 0 Alliance, 1 Horde (TeamId)',
    kind      VARCHAR(8)       NOT NULL COMMENT 'ours: our own people''s deeds; enemy: hearsay of the other side',
    hop       TINYINT UNSIGNED NOT NULL COMMENT '0 where it happened, one more at each stop along the road',
    origin    INT UNSIGNED     NOT NULL COMMENT 'The land where it happened',
    words     VARCHAR(255)     NOT NULL,
    told_at   DATETIME         NOT NULL COMMENT 'When this telling reached its places',
    spread_at DATETIME         NULL COMMENT 'When it was retold further on or its road ended; NULL while it may travel',
    KEY by_entry (entry_id),
    KEY waiting (spread_at, told_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS chronicle_rumour (
    id         BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    entry_id   BIGINT UNSIGNED  NOT NULL,
    tale_id    BIGINT UNSIGNED  NULL COMMENT 'The telling going around here (chronicle_tale)',
    zone_id    INT UNSIGNED     NOT NULL COMMENT 'Where the rumour is going around (AreaTable zone id)',
    team       TINYINT UNSIGNED NOT NULL COMMENT 'Who hears it: 0 Alliance, 1 Horde (TeamId)',
    distance   TINYINT UNSIGNED NOT NULL COMMENT 'Hops from where it happened: 0 there, then one more per stop',
    words      VARCHAR(255)     NOT NULL,
    expires_at DATETIME         NOT NULL,
    KEY by_place (zone_id, team, expires_at),
    KEY by_entry (entry_id),
    KEY by_tale (tale_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- The personal edition: one player's own characters (their main and alts, person_kind), watch by watch.
-- Written from their deeds alone by a scribe of no faction; nothing here becomes a rumour.
CREATE TABLE IF NOT EXISTS chronicle_personal (
    id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    window_start DATETIME     NOT NULL COMMENT 'Start of the watch, local time',
    window_end   DATETIME     NOT NULL,
    main_guid    INT UNSIGNED NOT NULL COMMENT 'Whose household: player_main.main_guid',
    scribe       VARCHAR(64)  NOT NULL,
    title        VARCHAR(160) NOT NULL,
    body         TEXT         NOT NULL COMMENT 'Empty for a quiet watch: none of them did anything worth a line',
    facts        JSON         NOT NULL COMMENT 'The reports the scribe wrote from',
    model        VARCHAR(64)  NOT NULL,
    era          VARCHAR(16)  NOT NULL,
    flag         VARCHAR(255) NULL COMMENT 'Judge evidence, when the last attempt was still flagged (kept for review)',
    created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY by_watch (window_start, main_guid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
