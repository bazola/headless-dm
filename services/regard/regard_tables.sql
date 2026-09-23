-- Regard (custom wow plans/14, step A2): how each bot feels about the people it has dealt with.
-- Scored by /opt/wow/regard/regard.py from mod-ledger's tables. Read by mod-ollama-chat (local patch)
-- into prompts, and by the dashboard through regard.json. Lives in acore_characters.

CREATE TABLE IF NOT EXISTS regard (
    bot_guid        INT UNSIGNED NOT NULL COMMENT 'The bot who feels',
    other_guid      INT UNSIGNED NOT NULL COMMENT 'Whom it is about: a bot or a real player',
    score           FLOAT        NOT NULL DEFAULT 0 COMMENT '-100 hatred .. 100 devotion',
    baseline        FLOAT        NOT NULL DEFAULT 0 COMMENT 'Where the score drifts back to: guild mates warm, other faction cool',
    familiarity     INT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Moments counted between them; only grows',
    last_reason     VARCHAR(160) NULL,
    last_delta      FLOAT        NULL,
    description     VARCHAR(255) NULL COMMENT 'One sentence in the bot''s own words',
    described_score FLOAT        NULL COMMENT 'Score when the description was written',
    decayed_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (bot_guid, other_guid),
    KEY by_other (other_guid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS regard_log (
    id         BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    ts         DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    bot_guid   INT UNSIGNED    NOT NULL,
    other_guid INT UNSIGNED    NOT NULL,
    delta      FLOAT           NOT NULL,
    score      FLOAT           NOT NULL COMMENT 'Score after this change',
    source     VARCHAR(8)      NOT NULL COMMENT 'chat or event',
    reason     VARCHAR(160)    NOT NULL,
    ledger_id  BIGINT UNSIGNED NULL COMMENT 'ledger_chat.id or ledger_event.id',
    KEY by_pair (bot_guid, other_guid, id),
    KEY by_ts (ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS regard_cursor (
    source  VARCHAR(8)      NOT NULL PRIMARY KEY COMMENT 'chat or event',
    last_id BIGINT UNSIGNED NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
