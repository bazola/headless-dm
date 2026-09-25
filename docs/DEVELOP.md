# What is on `develop`

> This branch runs ahead of `main`. It is not released and not merged: `main` is still the version the
> setup runbook was written against, and nothing here changes how a realm is installed. Everything on
> this branch is additive — a realm built from `main` keeps working exactly as it did, and every new
> key below defaults to off or is ignored by an older worldserver.

Read `docs/GUIDE.md` for setting a realm up in the first place. This page only says what is **new here**
and how to run it.

## Getting this branch

```bash
git clone --recursive -b develop https://github.com/bazola/headless-dm.git
```

The `mod-dashboard` submodule is pinned on this branch to **its own `develop` commit**, so a recursive
clone gets the dashboard's half of the journey stories with it. `.gitmodules` still names `main` as
that submodule's branch, which is right for `main` and wrong here: `git submodule update --remote`
would walk the dashboard back to `main` and the Stories button would vanish. Use plain
`git submodule update`, which follows the pin.

---

## 1. The story of a journey

The dashboard's **Journey** view shows one character's whole record in the order it happened. This adds
a **Stories** button to it: the record is cut into the stretches the character actually lived, and any
of them can be handed to the chronicler's scribes and written up as prose — kept, read back later, and
written again if the telling was poor.

**What counts as one journey.** The ledger has no logout event, and `characters.logout_time` keeps only
the *latest* logout rather than a history, so a log-off cannot be read for a journey that has already
happened. A journey is therefore bounded by what *is* recorded:

- the company breaking up, someone walking away from it, or being put out of it; or
- three quarters of an hour in which nothing happened.

Inside a journey, a **bout** is a stretch in one place with no gap longer than half an hour. One model
call per bout, stitched into one story, so a long night gets the length it earns and no single prompt
grows past what the model can hold.

### Running it from the command line

`services/chronicler/journey_story.py` needs no service of its own; it reads the ledger and calls the
batch lanes directly.

```bash
. ops/env.sh

# every journey this character has lived, newest first, and which are already written up
python3 services/chronicler/journey_story.py legs --guid <guid>

# write one up. --start is the journey's first moment, exactly as `legs` printed it
python3 services/chronicler/journey_story.py write --guid <guid> --start "2026-09-18 20:00:12"

# rebuild the file the dashboard reads, without writing anything new
python3 services/chronicler/journey_story.py publish --guid <guid>
```

`write` prints the story when it is done. Writing the same journey again **replaces** it and counts
`version` up rather than storing a second telling.

### Running it from the dashboard

The button posts to the **lore gate**, not to the worldserver — mod-dashboard caps a request body at
1024 bytes, answers no CORS preflight, and drains its commands on the world thread, which must never
wait on a model. Two endpoints were added to the gate:

```
GET  /journey-legs?guid=<guid>     the journeys this character has lived
POST /journey-write                {"guid": <guid>, "start": <unix seconds>} -> a job id
GET  /job?id=<job>                 how that writing is coming along (already existed)
```

Both refuse anyone who is not one of your own characters (`person_kind.kind` of `main` or `alt`).
Companions are named *inside* a story, but a story is never written *for* a wandering bot.

**The gate must be restarted for these to exist:**

```bash
systemctl --user restart wow-lore-gate
```

The page itself needs only a reload — web files are served from disk. In the dashboard: open a
character's Journey, press **Stories**. Reading a story back needs no token; **writing** one needs the
lore-gate token, the same one the Lore panel uses.

### What it stores

`journey_story`, created by `services/chronicler/journey_tables.sql` the first time the script runs, in
`acore_characters`. The dashboard reads `dashboard-data/journey-stories/<guid>.json` — its own
directory, deliberately not `journeys/`, which the regard service sweeps of anything it did not write.

---

## 2. Quest words

`services/lore/gen_quest_words.py` (+ `services/lore/quest_tables.sql`) retells quest text in the words
someone living in the world would use, so a raw quest title never reaches a bot's mouth verbatim. It is
generated once per quest, the way place names are, rather than per telling.

```bash
. ops/env.sh
python3 services/lore/gen_quest_words.py --help
```

Switched on with `OllamaChat.QuestWords.Enable` (see §4).

---

## 3. Backfilling memories no longer takes the realm down

`ops/scripts/backfill-memories.sh` used to insist the worldserver be stopped first. **That warning was
true when written and has been false since the memory save became insert-only.** The module makes only
two SELECTs and one INSERT against `mod_ollama_chat_memories` — no DELETE, no UPDATE — and rows read
out of the table are marked as already persisted, so a row the script writes cannot be overwritten by
the running server. The script says so itself now, and no longer tells you to shut the realm down.

If you have been following the old instruction, you can stop.

---

## 4. New configuration keys

In `conf/mod_ollama_chat.overrides`. All of them are ignored by a worldserver built from `main`, so the
conf is safe to carry either way.

| Key | What it does |
|---|---|
| `OllamaChat.RepeatPenalty` | Sampling penalty for repetition, measured to lengthen sentences |
| `OllamaChat.Places.Enable` / `.Chance` / `.Era` | The almanac: what a character knows about where they are standing |
| `OllamaChat.QuestWords.Enable` / `.Era` | Use the retold quest words of §2 instead of raw quest text |
| `OllamaChat.StripPersonaSheet` | Keeps the persona sheet out of the line that is spoken aloud |
| `OllamaChat.Memory.PromptTemplate` | Reworded so a character speaks a memory rather than reciting it |
| `OllamaChat.Snapshot.TheirTasks` | Now `0`: a character no longer recites a stranger's errands back at them |

A conf change needs `.ollama reload` on the worldserver console, except where the GUIDE says a restart.

---

## 5. Also on this branch

- `services/lore/era.py` — a few more later-age names the era gate should catch.
- `services/lore/gen_places.py` — the place almanac's generator, considerably extended.
- `services/regard/areas.py` — reads zone names from `AreaTable.dbc`, which is the only complete source
  of place names on the box (the `*_dbc` tables ship empty; the core reads the files directly).

## 6. Known rough edges

- Nothing writes a story unattended. Every one is a button press or a command, on purpose: a household
  with dozens of journeys is real inference time.
- A story is written per character, not per company, so two people on the same night are written up
  twice from two sides rather than once.
- The scribe is handed raw party chat, so an odd line is occasionally paraphrased oddly.
- There is no link from a story back to the stretch of the record it was written from.
