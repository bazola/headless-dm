# CleanBot: the Catch Up button (custom wow plans/27 C6, plans/28 W6)

**Where it lives now (2026-09-16).** This change is a commit on the `custom-wow` branch of the private repo
`bazola/CleanBot` (origin; `upstream` = bennybroseph/CleanBot; update = rebase, as for mod-playerbots). The client's
`interface/addons/CleanBot` is a clone of it. This file stays as the description of the change.

**Why this file exists.** The HD client is git-ignored, so our change to a third-party addon is not under
version control. An addon update would wipe it without a trace. This records it so it can be re-applied, and so
the game PC can be brought in step.

**Where.** `interface/addons/CleanBot/Individual/Individual.lua`, in the Commands inner tab of the Individual
tab, where the selected character is in scope.

**Two edits.**

1. Capture the shared builder's return so the new button has something to anchor below:

```lua
-- was:  NS.CB_BuildPartyRaidCommands(commandsContent, tag,
local cmdDeepest = NS.CB_BuildPartyRaidCommands(commandsContent, tag,
```

2. Immediately after that call's closing line
   (`function() return { slot } end)   -- gear commands refetch the open bot's equipment`), add:

```lua
    -- Catch Up (custom wow plans/27): raise one of YOUR OWN account's characters to your level so they can
    -- travel with you. Unlike every other button here this is a ".playerbots" dot-command in SAY rather than a
    -- whisper to the bot, so it follows Recruiter.lua's pattern, not CB_SendBotCommand's. The command comes
    -- FIRST and the name second: the other order reports "Character 'Catchup' not found".
    local catchUpBtn = NS.CB_CreateButton(commandsContent, "CleanBotCatchUpBtn_" .. tag,
        "Catch Up", 120, 24, function()
            local e  = CleanBot_PartyBots[slot.key]
            local bn = (e and e.name) or slot.name
            if not bn then return end
            SendChatMessage(".playerbots bot catchup " .. bn, "SAY")
            NS.CB_Print(bn .. " is catching up to your level. Follow with Maintenance, then Auto Gear.")
        end)
    NS.CB_SetTooltip(catchUpBtn, "Catch Up",
        "Raises one of your own account's characters to your level so they can travel with you. It grants the "
        .. "level and nothing else: no gear is touched, no bags are emptied, no quest is completed. Follow it "
        .. "with Maintenance (skills, spells, talents, mounts) and then Auto Gear.")
    if cmdDeepest then NS.CB_AnchorBelow(catchUpBtn, cmdDeepest) end
```

**Why it is not built like its neighbours.** Every other button there whispers a bot command through
`CB_SendBotCommand`. This one is a `.playerbots` dot-command and must go out in SAY, which is
`Recruiter.lua`'s pattern. The command comes **first** and the character second: the other order reports
`Character 'Catchup' not found`.

**On the game PC:** `git clone git@github.com:bazola/CleanBot.git` into `Interface/AddOns` (the default branch is
`custom-wow`), or `git pull` if it is already a clone, then `/reload`.
