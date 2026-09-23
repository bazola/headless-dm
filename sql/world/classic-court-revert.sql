-- Revert classic-court.sql (for the Northrend flip, or to undo). acore_world. Live after a worldserver restart.

UPDATE `creature` SET `phaseMask` = 1 WHERE `phaseMask` = 16384 AND `guid` IN (10495, 1976212, 49590);

DELETE FROM `creature` WHERE `guid` IN (5000001, 5000002);

DELETE FROM `creature_queststarter` WHERE `id` = 1748 AND `quest` = 6187;
DELETE FROM `creature_questender`   WHERE `id` = 1748 AND `quest` IN (6186, 6187, 7781);
DELETE FROM `creature_queststarter` WHERE `id` = 29611 AND `quest` IN (396, 6182, 6187, 7782);
DELETE FROM `creature_questender`   WHERE `id` = 29611 AND `quest` IN (396, 6186, 6187, 7781);
INSERT INTO `creature_queststarter` (`id`, `quest`) VALUES (29611, 396), (29611, 6182), (29611, 6187), (29611, 7782);
INSERT INTO `creature_questender`   (`id`, `quest`) VALUES (29611, 396), (29611, 6186), (29611, 6187), (29611, 7781);
