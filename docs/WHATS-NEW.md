# What is new since the setup runbook

Everything below is on `main` (merged 2026-09-29; there is no longer a separate `develop` branch). It is all
additive: a realm set up by `docs/GUIDE.md` keeps working exactly as it did, and every new key defaults to off
or is ignored by an older worldserver. Read the GUIDE to set a realm up in the first place; this page says what
arrived after it was written and how to run it.

```bash
git clone --recursive https://github.com/bazola/headless-dm.git
```

Every submodule is pinned to its own `main` (or, for the forks, `custom-wow`) commit, as `.gitmodules` names
them, so `git submodule update --remote` and plain `git submodule update` now agree.

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

## 4. Voices: a journey read aloud (optional)

**Off unless you set a speech key.** A realm with no key sees its Journey view exactly as before: no
speakers, no voice controls, and the gate's voice doors refuse without calling any model.

With a key, every line of talk in the Journey view gets a small speaker:

- **The first time a character is heard, you cast them.** A local batch model writes a description of how
  they sound from their race, class, sex and personality; you edit it, hear a sample, and accept or
  decline. An accepted sample *is* the voice: every later line is cloned from it, so it never drifts.
- **A voice changes only when you ask.** **Change voice**, beside the name at the top of a journey, opens
  the caster with their voice now beside it and the words that made it in the box. Keep a new one and the
  lines already recorded in the old voice are listed below: **Re-record all** says them again in the new
  voice (with a Stop), or do them one at a time. Until a line is redone it counts as unrecorded, so a
  section never mixes two voices. The old voice is not kept, so there is no undo.
- **The speaker shows the state:** a faint ring for no voice yet, plain for a voice whose line is not
  recorded, solid for a recorded line. A recorded line plays from disk, with no call and no cost.
- **Each section** of the journey shows how many of its lines are recorded. **Record the rest** casts
  anyone still voiceless (by hand, one at a time; closing a caster skips them) and records the rest.
  A complete section gets **Play all**, which reads it through in order, and **Download**, a zip of every
  line as its own file, the section as one track twice (as it happened, and back to back), subtitles for
  both, and a manifest, for laying over video.

**The engine** is Fish S2.1 Pro through OpenRouter's speech endpoint, about $15 per million characters
(a typical line is a fraction of a cent). It is the one model there that both designs a voice from words
and clones from a clip; the others have fixed voices or ignore a description. Fish does not always honour
a character's sex, so every sample is pitch-checked and remade, up to six times, until it fits; if none
does, the caster says so.

### Turning it on

```bash
# site/secrets.env
OPENROUTER_API_KEY=sk-or-...

# optional, in site/site.env
#VOICE_KEY_ENV=OPENROUTER_API_KEY   # which secrets.env key to bill
#VOICE_MODEL=fish-audio/s2.1-pro
#VOICE_ENABLED=0                    # force voices off even with a key
```

Then restart the lore gate. **Download needs `ffmpeg`** on the server (`sudo apt install ffmpeg`).

### What it stores

- Tables `character_voice` and `voice_line` in the characters schema, created by the gate on start.
- Reference clips in `DATA_DIR/voices/` (not served). Recorded lines, samples and downloads in
  `DATA_DIR/dashboard-data/voices/`, which the dashboard serves; `index.json` there tells the page which
  lines are recorded, in the voice each speaker has now (`voice_line.ref_sha1`). Unaccepted samples and
  downloads are swept after a day.

---

## 5. New configuration keys

In `conf/mod_ollama_chat.overrides`. All of them are ignored by a worldserver built before these features, so the
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

## 6. Also new

- `services/lore/era.py` — a few more later-age names the era gate should catch.
- `services/lore/gen_places.py` — the place almanac's generator, considerably extended.
- `services/regard/areas.py` — reads zone names from `AreaTable.dbc`, which is the only complete source
  of place names on the box (the `*_dbc` tables ship empty; the core reads the files directly).

## 7. Known rough edges

- Nothing writes a story unattended. Every one is a button press or a command, on purpose: a household
  with dozens of journeys is real inference time.
- A story is written per character, not per company, so two people on the same night are written up
  twice from two sides rather than once.
- The scribe is handed raw party chat, so an odd line is occasionally paraphrased oddly.
- There is no link from a story back to the stretch of the record it was written from.

## 8. Also new since the last section was written

- **Model costs, per purpose** (`docs/accounting.md`, contributed in #1): an optional recorder at the router
  and batch lanes, a `wow-accounting` exporter, and the dashboard's **Costs** page. `ACCOUNTING_ENABLED=1` in
  `site/site.env`; for OpenRouter, give each backend `usage = { include = true }` in its `extra_body` so the
  provider reports what each call cost.
- **Every model call can be rented**: `[aliases]` in `fleet.toml` send a name the code uses to one of your
  backends, and the batch lanes and judges carry the API key, so a realm with no local hardware needs no code
  change (`site.example/fleet.toml`).
- **Rumours**: `chronicler.py rumours` writes `rumours.json` for the dashboard's **Rumours** panel, where
  each recent story went and the chat that carried it, inferred with a chance control.
- **Memory notes**: `services/memory/scrub_notes.py` (dry run by default) removes stored notes that were the
  memory prompt handed back rather than anything that happened.
