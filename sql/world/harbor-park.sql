-- Stormwind Harbor stays empty in the Classic era (plan 34 §3.5, plan 12 era). acore_world.
-- Revert at the Wrath flip: harbor-park-revert.sql.
--
-- Core update 2026_09_17_00.sql (upstream #27319) inserts 21 creature spawns, guids 83245-83265, on
-- map 0, zone 1519, area 4411 -- the Stormwind Harbor. The harbour is a WotLK-era addition to the
-- 3.3.5 map, and two of its three entries are WotLK-era creatures:
--
--   15214  Stormwind Harbor Guard        (Classic-era entry, but the harbour itself is not)
--   29152  Stormwind Harbor Official     (WotLK)
--   29712  Deckhand                      (WotLK)
--
-- Our era stance (plan 22, classic-court.sql) is that nothing after the current era has happened, so
-- the crowd is parked rather than the update skipped: skipping would leave acore_world out of step
-- with the core's own updates table, which the updater tracks.
--
-- Parking uses mod-progression-system's convention (Bracket_0): phaseMask 16384, a phase nobody is in.
--
-- ORDER MATTERS: these guids do not exist until the updater has applied 2026_09_17_00.sql, which it
-- does on the first start after the core bump. Run this file AFTER that start, not before.

UPDATE `creature` SET `phaseMask` = 16384
WHERE `phaseMask` = 1
  AND `guid` BETWEEN 83245 AND 83265
  AND `id` IN (15214, 29152, 29712);

-- Expect 21 rows changed. If it reports 0, the core update has not been applied yet.
SELECT ROW_COUNT() AS parked_expect_21;
