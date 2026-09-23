-- Classic court in Stormwind Keep (plan 22 X1, plan 12 era). acore_world. Live after a worldserver restart.
-- Revert at the Northrend flip: classic-court-revert.sql.
--
-- The 3.3.5 keep has King Varian, Lady Jaina Proudmoore and a draenei envoy at the throne, and no Bolvar or
-- Prestor spawns. In the Classic era the boy-king Anduin holds court with Regent Bolvar and Lady Prestor.
--
-- Parking uses mod-progression-system's own convention (Bracket_0): phaseMask 16384, a phase nobody is in.
-- Our spawns use guids 5000000-5099999 (custom-wow world overlay range).

-- 1. Park the later-age people at the throne.
UPDATE `creature` SET `phaseMask` = 16384 WHERE `phaseMask` = 1 AND `guid` IN
(
10495,   -- King Varian Wrynn (29611)
1976212, -- Lady Jaina Proudmoore, Ruler of Theramore (32346)
49590    -- Emissary Taluun (17103), starts "Travel to Darkshire" (9429) for draenei
);

-- 2. Seat the regent and Lady Prestor: Bolvar where Varian stood, Prestor where Jaina stood.
DELETE FROM `creature` WHERE `guid` IN (5000001, 5000002);
INSERT INTO `creature` (`guid`, `id`, `map`, `zoneId`, `areaId`, `spawnMask`, `phaseMask`, `equipment_id`,
  `position_x`, `position_y`, `position_z`, `orientation`, `spawntimesecs`, `wander_distance`, `currentwaypoint`,
  `curhealth`, `curmana`, `MovementType`, `npcflag`, `unit_flags`, `dynamicflags`, `ScriptName`, `VerifiedBuild`,
  `CreateObject`, `Comment`) VALUES
(5000001, 1748, 0, 0, 0, 1, 1, 1, -8441.42, 333.102, 122.679, 2.23167, 7200, 0, 0, 0, 0, 0, 0, 0, 0, '', 0, 0,
  'custom-wow: Highlord Bolvar Fordragon, regent (classic-court.sql)'),
(5000002, 1749, 0, 0, 0, 1, 1, 1, -8443.36, 331.838, 122.663, 1.85005, 7200, 0, 0, 0, 0, 0, 0, 0, 0, '', 0, 0,
  'custom-wow: Lady Katrana Prestor (classic-court.sql)');

-- 3. Varian's Classic duties go back to the court. Wrath quests keep their Varian links (parked with him).
--    396  An Audience with the King: Baros Alexston (1646) starts, Lady Prestor (1749) ends
--    6182 The First and the Last, 7782 The Lord of Blackrock: Bolvar already starts them
--    6186 The Blightcaller Cometh, 6187 Order Must Be Restored, 7781 The Lord of Blackrock: to Bolvar
DELETE FROM `creature_queststarter` WHERE `id` = 29611 AND `quest` IN (396, 6182, 6187, 7782);
DELETE FROM `creature_questender`   WHERE `id` = 29611 AND `quest` IN (396, 6186, 6187, 7781);
DELETE FROM `creature_queststarter` WHERE `id` = 1748 AND `quest` = 6187;
DELETE FROM `creature_questender`   WHERE `id` = 1748 AND `quest` IN (6186, 6187, 7781);
INSERT INTO `creature_queststarter` (`id`, `quest`) VALUES (1748, 6187);
INSERT INTO `creature_questender`   (`id`, `quest`) VALUES (1748, 6186), (1748, 6187), (1748, 7781);

-- C2 note: mod-progression-system Bracket_60_2_1 (the Masquerade) links The True Masters 4184 (ender) and
-- 4185 (starter) to Varian (29611). When C2 opens, re-point both to Bolvar (1748) after its SQL has run.
