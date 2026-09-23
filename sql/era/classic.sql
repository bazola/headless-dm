-- sql/era/classic.sql — hold every account at the Classic expansion (plan 12 §2.4, plan 23 W7).
--
-- An account's `expansion` decides which races and classes its characters may be made with, and how far
-- the client will let them go. Setting it to 0 is what makes a realm Classic to the people playing on it,
-- rather than a Wrath realm that everyone has agreed not to explore.
--
-- Run it against the AUTH database:
--     . ops/env.sh && db "$DB_AUTH" < sql/era/classic.sql
--
-- Re-runnable, and reversible by the same statement with a different number: 1 for TBC, 2 for Wrath.
-- Appendix C's era steps are one-way in the sense that the world data moves with them; this half is not.

UPDATE account SET expansion = 0;

-- What this file deliberately does NOT contain, checked against a live Classic realm 2026-09-22:
--
--   * Arena and battleground `disables`. Plan 12 §2.4 recorded "account expansion 0 + arena disables"
--     together, but this realm has no `disables` rows of sourceType 6 (battleground) at all — and never
--     had. Its 954 rows are sourceType 0/1/2/3/4/7: spells, quests, maps, and so on, of which the map
--     rows are mod-progression-system's era gating, which that module owns and applies for itself.
--     There was nothing to write down, so nothing is invented here. If a realm wants arenas closed, that
--     is a DISABLE_TYPE_BATTLEGROUND row per arena map, and it belongs in its own file with a revert.
--
--   * Any character-side change. Levels come from the bracket configuration, not from SQL.
--
-- Worth knowing before running it: on the realm this was written from, 87 of 158 accounts were still at
-- expansion 2 — 86 of them random-bot accounts created after the era was set. Setting the expansion once
-- at setup does not hold it: accounts made later arrive at the core's default. Re-run this after any
-- batch of bot accounts is created, which is what `add-bots.sh` does.
