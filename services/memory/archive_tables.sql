-- Permanent archive of bot memories (custom wow plans/41).
--
-- mod_ollama_chat_memories is NOT durable: Memory_SaveAll rewrites a dirty bot's rows as
-- DELETE-then-INSERT from RAM (mod-ollama-chat_memory.cpp:465-481), so anything RAM has
-- dropped is destroyed on the next write. These two objects keep a copy.
--
-- Dedup key is (bot_guid, created_at, MD5(memory_text)), never id: the delete-and-reinsert
-- regenerates AUTO_INCREMENT ids every save cycle, so the same memory carries a different id
-- each time. created_at survives the rewrite (FROM_UNIXTIME(m.createdAt)).
--
-- Apply to acore_characters. Safe to re-run.

CREATE TABLE IF NOT EXISTS mod_ollama_chat_memories_archive (
    archive_id  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    bot_guid    BIGINT UNSIGNED NOT NULL,
    memory_text TEXT NOT NULL,
    importance  TINYINT UNSIGNED NOT NULL DEFAULT 5,
    created_at  DATETIME NOT NULL,
    archived_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    text_hash   CHAR(32) AS (MD5(memory_text)) STORED,
    UNIQUE KEY uq_mem (bot_guid, created_at, text_hash),
    INDEX idx_bot (bot_guid),
    INDEX idx_archived (archived_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Append-only. mod_ollama_chat_memories is rewritten from RAM every save, so it is not durable.';

-- Catches evictions the moment they happen. INSERT IGNORE is deliberate: this runs inside the
-- worldserver's own save transaction, so a failure here must never abort a bot's memory save.
DROP TRIGGER IF EXISTS trg_ollama_mem_archive;
CREATE TRIGGER trg_ollama_mem_archive
AFTER DELETE ON mod_ollama_chat_memories
FOR EACH ROW
  INSERT IGNORE INTO mod_ollama_chat_memories_archive
    (bot_guid, memory_text, importance, created_at)
  VALUES (OLD.bot_guid, OLD.memory_text, OLD.importance, OLD.created_at);

-- Backfill whatever is live right now. Idempotent; the sweeper (archive.py) repeats it every 60s.
INSERT IGNORE INTO mod_ollama_chat_memories_archive (bot_guid, memory_text, importance, created_at)
SELECT bot_guid, memory_text, importance, created_at FROM mod_ollama_chat_memories;
