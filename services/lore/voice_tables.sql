-- Voices (plan 57): a character's voice, cast once and kept, and every line it has said aloud.
-- Lives in the characters schema beside the lore tables. The audio itself is on disk, never here.

-- One row per character who has been given a voice. A row is written only when the player accepts a
-- sample: the accepted sample IS the voice, and every later line is cloned from it. It is rewritten only
-- when the player asks to change the voice (the Journey page's "Change voice"); the lines recorded in the
-- old one then go stale (voice_line.ref_sha1 no longer matches) until they are recorded again.
CREATE TABLE IF NOT EXISTS character_voice (
    guid         INT UNSIGNED NOT NULL,
    name         VARCHAR(12)  NOT NULL,
    gender       TINYINT UNSIGNED NOT NULL COMMENT 'characters.gender: 0 male, 1 female',
    style        VARCHAR(400) NOT NULL COMMENT 'The words the voice was designed from, as the player left them',
    sample_text  VARCHAR(600) NOT NULL COMMENT 'What the reference clip says; the clone is told this',
    ref_file     VARCHAR(255) NOT NULL COMMENT 'The reference WAV, relative to DATA_DIR/voices',
    ref_sha1     CHAR(40)     NOT NULL,
    model        VARCHAR(80)  NOT NULL COMMENT 'The model that designed it and clones it',
    locked       TINYINT UNSIGNED NOT NULL DEFAULT 1,
    created_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Every line rendered, keyed by what makes it sound the way it does (model, reference clip, text), so
-- a line is paid for once and sounds the same every time it is played again.
CREATE TABLE IF NOT EXISTS voice_line (
    cache_key    CHAR(40)     NOT NULL,
    guid         INT UNSIGNED NOT NULL,
    text         VARCHAR(1000) NOT NULL COMMENT 'What was spoken, after clean-up',
    raw_text     VARCHAR(1000) NULL     COMMENT 'The line as the journey file holds it; what the page looks it up by',
    file         VARCHAR(255) NOT NULL COMMENT 'Relative to dashboard-data/',
    ref_sha1     CHAR(40)     NULL     COMMENT 'The reference clip it was cloned from; a line whose clip is not the voice now is stale',
    chars        INT UNSIGNED NOT NULL COMMENT 'Characters billed',
    created_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (cache_key),
    KEY by_guid (guid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
