# Headless DM — Building On It

> **Draft (2026-09-15, revised 2026-09-23)**, written against the release layout in
> `plans/23-PLAN-public-release.md` §3. Every command it names exists.

This page is for changing the realm: patching a module, adding a behaviour, a service or an era. Read
`docs/GUIDE.md` Appendix A first. The rules there aren't style preferences; each one exists because breaking it broke
something.

**Contents**
1. [Where a change belongs](#1-where-a-change-belongs)
2. [Working with the forks](#2-working-with-the-forks)
3. [The data flow you plug into](#3-the-data-flow-you-plug-into)
4. [Recipes](#4-recipes)
5. [Writing prompts for people, not players](#5-writing-prompts-for-people-not-players)
6. [Adding an era](#6-adding-an-era)
7. [Testing a change](#7-testing-a-change)
8. [Releasing](#8-releasing)

---

## 1. Where a change belongs

| You want to… | Put it in | Why |
|---|---|---|
| Record something that happens in game | `mod-ledger` (a script hook, one `ledger_event` row) | Append-only facts; everything else reads them |
| Change what a bot *does* (moves, accepts, duels, invites) | `mod-playerbots` fork, `custom-wow` branch | Bot behaviour lives in its actions and triggers |
| Change what a bot is *told* before it speaks | `mod-ollama-chat` fork, `custom-wow` branch | Prompt assembly, snapshots, templates |
| Decide something slowly from history (feelings, membership, stories) | A Python service under `services/` | Model calls and long queries stay off the world thread; results go to tables the modules reload |
| Show something to the operator | `mod-dashboard` (`/data/*.json` written by a service, a panel in `web/`) | Web files are served from disk; edits go live on reload |
| Change the world's spawns or quest links | `sql/world/<name>.sql` + `<name>-revert.sql` | Kept apart from bracket SQL; each file carries its own undo |
| Change a conf value | Live conf, then `ops/scripts/conf-overrides.py`, commit `conf/` | The overrides are the record |
| Touch the core | Don't | No core diff so far; find a module hook. If one is truly missing, raise it upstream |

---

## 2. Working with the forks

**Branches.** Each patched module has two:
- The upstream default branch (`master` / `main`), unchanged.
- `custom-wow`: our commits on top, one concern each.

**Remotes.** In each clone, `origin` is our fork and `upstream` is the public project.

```bash
cd src/modules/mod-playerbots
git log --oneline upstream/master..custom-wow      # our commits
git diff upstream/master...custom-wow --stat       # what they touch
```

**Marking changes.** Every changed site in upstream code carries a marker comment naming the feature, for example
`// local: company regard gate`. `grep -rn "// local:"` shows every patch point during a rebase.

### Updating from upstream

```bash
cd src/modules/mod-playerbots
git fetch upstream
git rebase upstream/master custom-wow
# resolve, rebuild, run the checks in §7
git push --force-with-lease origin custom-wow
cd ../../..
git add src/modules/mod-playerbots && git commit -m "mod-playerbots: rebase onto upstream <sha>"
```

**Move the core and mod-playerbots together.** The Playerbot core fork and mod-playerbots are released as a pair.
If a build breaks after an update, check both projects' recent commits before patching anything.

**On every mod-playerbots rebase, re-check the command mirror.** `OllamaIsCommandFromMaster` in mod-ollama-chat mirrors
how mod-playerbots parses chat commands (`PlayerbotAI::HandleCommand`, `ExternalEventHelper::ParseChatCommand`). If
that parsing changes, orders to your own bots start getting spoken replies.

### Patching mod-ollama-chat

**Its sources are CRLF with a byte-order mark.** Editors and scripts that rewrite the files in text mode convert every
line, and the diff becomes the whole file. Edit in binary-safe mode and check `git diff --stat` before committing.

---

## 3. The data flow you plug into

```
game ──hooks──► mod-ledger ──► ledger_event / ledger_chat (acore_characters, append-only)
                                        │
                     services read with a cursor (regard_cursor, per consumer)
                                        │
          ┌─────────────────────────────┼──────────────────────────────┐
     lore (batch)                  regard (120 s)                chronicler (600 s)
     lore_* tables                 regard, regard_log,           chronicle_entry,
     BIO_/BIOX_ templates          guild_seat/relation/holding,  chronicle_rumour
                                   company_action, guild_words
          │                             │                              │
          └──────────── modules reload on a detached thread, swap a shared_ptr ─────┘
                                        │
                  world thread reads the snapshot: prompts, bot decisions
```

### Conventions

- **Tables are created by the code that owns them.** Every service runs `CREATE TABLE IF NOT EXISTS` from its own
  `*_tables.sql` at startup. The modules check a table exists before loading it, so order doesn't matter and a
  missing service degrades to stock behaviour.
- **One transaction per cycle.** Side effects tied to ledger rows go in the same transaction as the cursor move
  (`regard.py commit(extra_sql=…)`), so a failed cycle can't apply them twice.
- **Modules ask, services act slowly.** A module that needs a decision reads a precomputed row: `company_action`,
  `regard`, `chronicle_rumour`. It doesn't call a model on the world thread.
- **Game-object work drains on the world thread.** HTTP threads and workers enqueue requests; the queue drains in
  `WorldScript::OnUpdate` (see mod-dashboard's command queue).
- **Text that shows up in more than one place.** A regard reason is read in prompts ("since …"), in the chronicler's
  reports and on the dashboard. Change its voice in one place and check the other two.

---

## 4. Recipes

### 4.1 A new model-driven behaviour in a service

1. **Kill switch first.** A file the operator can touch (`services/<svc>/NO_<THING>`) and/or a conf key that defaults
   to off.
2. **Chance gate.** A roll with the chance in config, so the behaviour can be turned down without being turned off.
3. **Pick a lane by role** from `fleet.toml`: `writer`, `novelist` or `judge`. Never by host.
4. **Check before writing.** Pattern checks first (later-era names, figures, game words), then the judge. Rejected
   texts go to `review-*.jsonl`, not the DB.
5. **Write rows, not game state.** The module reads your table on its next reload.
6. **Degrade quietly.** A missing backend means the work waits for the next cycle; nothing crashes. Backends can leave
   the pool at any time.

### 4.2 A new prompt input in mod-ollama-chat

1. **Loader.** Add it to the regard thread's reload (`src/mod-ollama-chat_sentiment.cpp`): check the table exists, read
   it, build words, and swap the snapshot.
2. **Section function.** Returns an empty string when disabled or when nothing applies.
3. **Call sites.**
   - `GenerateBotPrompt` in `_handler.cpp` (replies).
   - `_random.cpp` (chatter) and `_events.cpp` (events), each behind a chance.
4. **Config.** Keys in `_config.h/.cpp`, defaulting to off, documented in `conf/mod_ollama_chat.conf.dist`.
5. **Record it:** a `// local:` marker at each site, and a commit on `custom-wow`.

### 4.3 A new bot behaviour in mod-playerbots

**Where the code goes:**
- **Snapshot.** Load what the behaviour needs in `src/Bot/CompanyStanding.cpp`, or a sibling file. It is reloaded on a
  detached thread every `AiPlayerbot.CompanyRefreshSeconds`.
- **Decisions** happen in the relevant action (for example `src/Ai/Base/Actions/*`, or the rpg actions in
  `src/Ai/World/Rpg/Action/`), with a chance key defaulting to 0.
- **Real players vs bots.** Decide whether the behaviour applies to real players, bots or both. The regard gate weighs
  only real players' charters and invites, and bots keep stock answers.
- **New `.cpp` files** need `cmake .` in `build/` before `make`.

The stock "start duel" strategy has no triggers under the new rpg strategy. That's why rival duels are started from
the wander actions instead.

### 4.4 A new ledger event

1. **Hook.** Add it in mod-ledger: a `PlayerScript`, `GuildScript`, `UnitScript` or packet hook.
2. **Where the core has no hook, look at packets.** `ServerScript::CanPacketReceive(WorldSession*, WorldPacket const&)`
   sees a packet before its handler runs. That's how guild declines are recorded.
3. **Row.** Insert asynchronously with every string escaped.
4. **Offline actors.** Write them by guid, with `actor_is_bot` taken from the random-bot account list.
5. **Consume it** in a service through the cursor.

### 4.5 A new service

1. **Code.** `services/<name>/<name>.py` with `run --interval N`, `once`, and `--dry-run`. Settings come from the
   shared loader (`site.env`, `secrets.env`, `fleet.toml`).
2. **Schema.** `services/<name>/<name>_tables.sql`, applied by the service at start.
3. **Unit.** `ops/user-units/wow-<name>.service`.
4. **Pause.** A `PAUSE` file, checked at the top of each cycle.
5. **Operator output.** Optional `/data/<name>.json` for the dashboard.

### 4.6 A world overlay

1. **Dump first:**
   ```bash
   mysqldump -uacore -p acore_world > "$BACKUP_DIR/<name>-$(date +%F).sql"
   ```
2. **Write** `sql/world/<name>.sql` and `<name>-revert.sql`.
3. **Park, don't delete.** Set `phaseMask = 16384` on spawns you remove.
4. **New spawns** take guids from 5000000–5099999.
5. **Leave `ProgressionSystem.ReapplyUpdates` at 0,** so bracket SQL doesn't run over your rows.
6. **Find candidates.** `ops/scripts/era-spawn-sweep.py --zone "<zone>"` lists spawns that don't belong to the era.

---

## 5. Writing prompts for people, not players

Bots in this realm are people who live in Azeroth. Everything a bot is told should be something that person could know.

| Instead of | Say |
|---|---|
| "HP 23%", "level 34", "12 gold" | "badly hurt", "a seasoned fighter", nothing about money unless it matters |
| "quest", "mob", "respawn", "aggro", "NPC" | "a task you took on", "the beasts", — , "it turned on you", a name or a trade |
| "leveled up" | "grew stronger" |
| Player-facing reference text | The same facts told by someone who lives there (`rag_inworld.py`) |

**Era.** Nothing after the current era has happened. Put era checks in patterns you can grep, and give the judge an
allowed list, not just a forbidden one: small models flag legitimate Classic things unless told they're fine.

**Model quirks seen here:**

| Quirk | What to do |
|---|---|
| Invented names repeat (one name in 22% of stories) | Watch the traits report |
| Stock openings repeat | Watch the traits report |
| Towns end up in the wrong land | List each land's settlements in the prompt |
| Living company members get killed off | Tell the model they're alive |
| A model is asked how seasoned a story reads | Don't: epic backstories read seasoned at any level, so use evidence |

---

## 6. Adding an era

What moving to The Burning Crusade needs in code (config and SQL are in GUIDE Appendix C):

| Where | What |
|---|---|
| `services/lore/era.py` | A `tbc` entry: the setting text, the pattern for later-age names, the judge's allowed and forbidden lists, `max_level`, targets |
| `services/regard/lands.py` | Outland zones with level ranges and settlements; `rivalry.py seed` exits without them |
| `services/chronicler/chronicler.py` | Outland geography (continents, capitals) |
| `services/lore/gen_backstories.py` | A `chapter` command. `generate` refuses to run while rows of another era exist; a chapter adds to each story without rewriting it |
| `services/lore/rag_inworld.py` | Restore the parked entries |
| `sql/era/tbc.sql` | Account expansion 1; remove the arena disables |
| `sql/world/` | The court and other overlays for the era, with reverts of the previous era's |
| `ops/scripts/lore-fill.sh`, `add-bots.sh`, `society.py` | They read `ERA`; check their race and class filters |

---

## 7. Testing a change

**Default off, then on.** A build with your keys at their defaults must behave like before.

**Service changes:**
- `--dry-run` or `sample` first.
- Test against fake rows in a scratch database, or mock the ledger. Check the SQL a step would run without running it.

**Prompt changes.** Read a live prompt, not just the code. Bots speak in chatter, replies and events, and each builds
its own prompt.

**Live checks.** Pick a real character, give the change a few cycles, and read the dashboard and the ledger.

**What only a restart shows:** guild info, rank names, the knowledge base, world SQL and C++. Say in the commit what
is waiting on a restart.

**In game:** some paths (invites, declines, charters) only happen with a real player. List what you couldn't test.

---

## 8. Releasing

1. **Commit in each submodule and push its branch.**
2. **Bump the gitlinks in the superproject.** `git submodule status` must show no `+`.
3. **Tag the superproject:** `git tag -a v<date> -m "…"`, then push the tag.
4. **Write release notes:**
   - what changed, per repository
   - which conf keys are new (with defaults)
   - which tables are new
   - whether a restart or a rebuild is needed
   - any one-way step (bracket SQL, world overlays) with its backup instruction
