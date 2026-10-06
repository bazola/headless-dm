-- The Dungeon Master's scene channel (custom wow plan 62). acore_characters.
--
-- dm_line: what the world will say, written ahead by services/dm/dm.py and spoken by mod-ollama-chat
-- (mod-ollama-chat_director.cpp), which loads it on OllamaChat.Director.RefreshSeconds and NEVER writes
-- back. Delivery is recorded in ledger_event as `dm_scene` (detail.line = dm_line.id), so this table keeps
-- one writer (plan 32 section 3).
--
--   scene          'boss_approach' (a dungeon's final boss, as the party comes into sight)
--                  'inn_gossip'    (any innkeeper in zone_id, to for_guid, as they walk up; plan 62 1d)
--   instance_id    0 = for anyone (the fallback lines, `dm.py boss-words`); else the instance it was written for
--   speaker_entry  creature_template entry of who speaks
--   audience       JSON array of the guids it was written for; [] = anyone
--   for_guid, zone_id   who and where an inn line is for (0 on boss lines). dm.py adds both to an older table
--   `rank`         best first (0); `rank` is a reserved word in MySQL 8, so always quote it
--   why            the evidence it was written from, in plain words, for the dashboard and for audits

CREATE TABLE IF NOT EXISTS `dm_line` (
  `id` BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  `scene` VARCHAR(24) NOT NULL,
  `instance_id` INT UNSIGNED NOT NULL DEFAULT 0,
  `map_id` INT UNSIGNED NOT NULL,
  `speaker_entry` INT UNSIGNED NOT NULL,
  `audience` JSON NULL,
  `for_guid` INT UNSIGNED NOT NULL DEFAULT 0,
  `zone_id` INT UNSIGNED NOT NULL DEFAULT 0,
  `words` VARCHAR(255) NOT NULL,
  `why` VARCHAR(512) NULL,
  `rank` TINYINT UNSIGNED NOT NULL DEFAULT 0,
  `cue_id` BIGINT UNSIGNED NULL,          -- the ledger_event (instance_enter) it answers; NULL for a fallback
  `model` VARCHAR(64) NULL,
  `made_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `expires_at` DATETIME NULL,
  KEY `by_scene` (`scene`, `instance_id`, `speaker_entry`),
  KEY `by_player` (`scene`, `for_guid`, `zone_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- How far dm.py has read ledger_event. Its own table: regard_cursor belongs to regard.py.
CREATE TABLE IF NOT EXISTS `dm_cursor` (
  `source` VARCHAR(32) PRIMARY KEY,
  `last_id` BIGINT UNSIGNED NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
