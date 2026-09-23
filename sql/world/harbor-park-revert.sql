-- Undo harbor-park.sql: let the Stormwind Harbor crowd appear (plan 34 §3.5). acore_world.
-- Run this at the Wrath flip, when the harbour is in its own era.

UPDATE `creature` SET `phaseMask` = 1
WHERE `phaseMask` = 16384
  AND `guid` BETWEEN 83245 AND 83265
  AND `id` IN (15214, 29152, 29712);

SELECT ROW_COUNT() AS unparked_expect_21;
