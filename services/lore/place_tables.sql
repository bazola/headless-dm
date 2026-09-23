-- Places (custom wow plans/46): what each land is like, in the words of people who live there.
--
-- Written by services/lore/gen_places.py on the operator's own machine, from the operator's own realm, and
-- read by mod-ollama-chat into prompts the way land_words and guild_words are (plan 14 B3). Lives in
-- acore_characters beside land_words (services/regard/rivalry_tables.sql).
--
-- NOTHING ABOUT AZEROTH IS COMMITTED TO THE REPOSITORY. The zone list comes from the realm's own
-- ledger_event kills, the names from the client's AreaTable.dbc, and every sentence is generated here and
-- stored here. Two realms produce two different almanacs and neither one ships.
--
-- Four rows per zone per era, on four different angles rather than four paraphrases. One is chosen at
-- random per prompt. A single cached description handed identically to every bot in a zone is exactly the
-- shape that produced "the dead do not" across 46 bots (plan 37), which is why `variant` is in the key.

CREATE TABLE IF NOT EXISTS place_words (
    zone_id    INT UNSIGNED    NOT NULL COMMENT 'AreaTable zone id, as ledger_event.zone_id records it',
    era        VARCHAR(16)     NOT NULL COMMENT 'Silithus before and after the war are not the same place',
    variant    TINYINT UNSIGNED NOT NULL COMMENT 'zero upwards - one is drawn at random per prompt',
    angle      VARCHAR(16)     NOT NULL COMMENT 'sight | danger | people | grievance',
    words      VARCHAR(255)    NOT NULL COMMENT 'The place as a pressure, not an encyclopaedia entry',
    updated_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (zone_id, era, variant),
    KEY by_era (era)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
