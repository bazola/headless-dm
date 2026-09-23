# CleanBot: no Recruit (custom wow BUILD_INFO "No addclass")

**Where it lives now (2026-09-16).** This change is a commit on the `custom-wow` branch of the private repo
`bazola/CleanBot` (origin; `upstream` = bennybroseph/CleanBot; update = rebase, as for mod-playerbots). The client's
`interface/addons/CleanBot` is a clone of it. This file stays as the description of the change.

**Why this file exists.** The HD client is git-ignored, so this change to a third-party addon is not under version
control, and an addon update would silently bring Recruit back. It records the change so it can be re-applied and
so the game PC can be brought in step. The server refuses `addclass` regardless; this only removes a button that
would now just print a refusal.

**What Recruit did.** `.playerbots bot addclass <class>` pulled a character from the 500 on the AddClass accounts
and rebuilt it from scratch at the player's level (gear, bags, spells, talents, quests), with a level-1 backstory or
none at all. That has no place in a world where every character has a life of their own.

**Three edits** in `interface/addons/CleanBot` (pre-change copy: `/opt/wow/backups/cleanbot-20260916-pre-norecruit`):

1. `CleanBot.toc`: delete the `Recruiter.lua` line, and delete the file itself (the Dungeon Finder tab).
2. `ActionBar.lua`: delete the `recruit` entry from `SLOTS` (the nested Recruit flyout on the action bar). The
   builder functions above it (`CB_BuildRecruitTree`, `CB_CreateRecruitButton`) stay; nothing calls them once the
   slot is gone. A saved bar layout that still names `recruit` is harmless: `CB_MergeOrder` drops unknown ids.
3. `SettingsTab.lua`: delete the "Dungeon Finder" header and "Show Recruiter Tab" checkbox, and anchor the
   "Action Bar" header below `hideChatterCB` instead of `recruiterCB`.

**After an addon update:** `grep -rn addclass` over the addon should find nothing but comments.

**On the game PC:** `git clone git@github.com:bazola/CleanBot.git` into `Interface/AddOns` (the default branch is
`custom-wow`), or `git pull` if it is already a clone, then `/reload`.
