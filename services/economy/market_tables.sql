-- The market's memory (custom wow plans/17 §3.E), acore_characters. Written by services/economy/market.py.
-- Readers: mod-ah-bot-plus (AuctionHouseBot.MarketPrices.*) and mod-playerbots (bots' own listings) read
-- market_price; mod-ollama-chat (OllamaChat.Market.Talk) reads market_word.

CREATE TABLE IF NOT EXISTS `market_price` (
  `item_entry` INT UNSIGNED NOT NULL PRIMARY KEY,
  `unit_copper` INT UNSIGNED NOT NULL COMMENT 'learned price for one item',
  `list_copper` INT UNSIGNED NULL COMMENT 'median calculated merchant listing for one item when last learned',
  `samples` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'sales, sold and expired listings behind the price in the window',
  `updated_at` DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `market_word` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `zone_id` INT UNSIGNED NOT NULL COMMENT 'AreaTable zone of the market city',
  `team` TINYINT UNSIGNED NOT NULL COMMENT '0 Alliance, 1 Horde, 2 anyone (neutral houses)',
  `item_entry` INT UNSIGNED NOT NULL,
  `words` VARCHAR(255) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'an in-world fact with no numbers',
  `expires_at` DATETIME NOT NULL,
  KEY `by_place` (`zone_id`, `team`, `expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
