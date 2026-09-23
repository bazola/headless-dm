# Headless DM — Setup Runbook

> **Draft (2026-09-15, revised 2026-09-23).** Written for the release layout in
> `plans/23-PLAN-public-release.md` §3. Every command it names exists. This is how the reference realm
> was built.

This runbook takes a fresh Ubuntu 24.04 machine to a living realm. It is written for a **coding agent working with a
human operator**: the agent runs the steps, and the operator does what only a person can do. A person can follow it
alone, too.

**What gets built.** An AzerothCore 3.3.5a realm where hundreds of playerbots are voiced by language models as people
who live in Azeroth.
- Each bot has a backstory, a temperament and a motivation.
- Bots hold feelings about the people they meet.
- They belong to companies that hold land and keep rivalries.
- They pass on rumours written by faction scribes.
- The world follows a release schedule: Classic, then The Burning Crusade, then Wrath of the Lich King.

**What doesn't come with it.** No generated data ships. Your realm writes its own histories, backstories, feelings and
chronicle with your models. No Blizzard data ships either: you bring your own client.

**Contents**
- [Part 0 — For the agent](#part-0--for-the-agent)
- [Phase 1 — Survey the machine](#phase-1--survey-the-machine) · G1
- [Phase 2 — Source and site files](#phase-2--source-and-site-files) · G4
- [Phase 3 — Dependencies](#phase-3--dependencies) · G3
- [Phase 4 — Build](#phase-4--build)
- [Phase 5 — Databases](#phase-5--databases) · G3
- [Phase 6 — Client data](#phase-6--client-data) · G2
- [Phase 7 — Configure and first boot](#phase-7--configure-and-first-boot) · G3 G6
- [Phase 8 — Models and router](#phase-8--models-and-router) · G3 G5
- [Phase 9 — First contact](#phase-9--first-contact) · G8
- [Phase 10 — Lore](#phase-10--lore) · G3 G10
- [Phase 11 — Companies, society, chronicle, the era's world](#phase-11--companies-society-chronicle-the-eras-world) · G3 G9
- [Phase 12 — Scale to the target](#phase-12--scale-to-the-target) · G3 G7
- [Phase 13 — Hand over](#phase-13--hand-over) · G3
- Appendices: [A Architecture](#appendix-a--architecture-and-design-rules) ·
  [B Operating](#appendix-b--operating) · [C Eras](#appendix-c--eras) ·
  [D Troubleshooting](#appendix-d--troubleshooting) · [E Licenses](#appendix-e--licenses-and-credits)

---

## Part 0 — For the agent

### 0.1 How the phases work

Every phase has the same parts, in this order:

| Part | Meaning |
|---|---|
| **Needs** | What must already be true. If it isn't, go back; don't patch around it |
| **Steps** | Commands to run. Run them as written, adapting only the values from `site/` |
| **Gate** | Where the operator acts or decides. Stop, ask, wait for the answer |
| **Check** | Commands whose output proves the phase worked. **Every check passes before the next phase starts** |
| **Record** | What to write in `site/SETUP-STATE.md` |

Read Appendix A before Phase 1. It explains what you are building and the rules the design keeps.

### 0.2 Rules

1. **Never run `sudo` yourself.** Give the operator one short command at a time: long pastes wrap and break in
   terminals. Wait for them to report the result.
2. **Secrets stay in `site/secrets.env`.** Never print one, write one elsewhere, or put one on a command line that
   shows up in a process list or log. Use the `db` and `ra` helpers (0.5), which read them from the file.
3. **Never copy, commit or upload client files or extracted data.** They are Blizzard's.
4. **Back up before anything one-way:** bracket SQL, world overlays, conf changes. Say where the backup is.
5. **On a failure, stop.** Report the exact error and the command that produced it. Don't work around a failure this
   runbook doesn't cover. Bot pathing trouble is almost always bad data, not code (Appendix D).
6. **Don't change code during setup.** If a step needs a code change to work, that's a runbook bug: record it under
   Open problems and stop.
7. **Heavy jobs run under `nice`** (builds, extraction, generation), especially once someone is playing.
8. **Bots are people living in Azeroth, never players.** Anything you write that a bot will read follows Appendix A.
9. **This realm trusts everyone who can reach it.** There is no per-user login. Anyone who can open the dashboard can
   read every household's lore, chronicle and journey, and anyone holding the lore-gate token can write for any
   account. Bind the dashboard and the gate to localhost or a private network, and give the token only to someone you
   would give the GM console to. Say this to the operator in plain words before Phase 13 — it decides who they let in,
   and it is not something to discover later.

### 0.3 Operator gates

| Gate | Phase | The operator… |
|---|---|---|
| **G1 Machine** | 1 | Confirms what you measured; chooses the install root, the account the servers run as and the bot target |
| **G2 Client** | 6 | Puts their own 3.3.5a (build 12340, enUS) client in place and confirms its build |
| **G3 sudo** | 3, 5, 7, 8, 10, 11, 12, 13 | Runs each privileged command you give them |
| **G4 Secrets** | 2 | Chooses the GM console account and password and types them into `site/secrets.env` |
| **G5 Models** | 8 | Says what model servers exist or can be started, where, and what they can spare |
| **G6 First boot** | 7 | Runs the first boot in their own terminal and creates the accounts at the console |
| **G7 Scale** | 1, 12 | Confirms each step up in bot count |
| **G8 In game** | 9 | Logs in, talks to a bot, says what came back |
| **G9 One-way** | 11, Appendix C | Approves world overlays and era changes |
| **G10 The main** | 10.5 | Names the character they play as themselves — **or says explicitly that this realm has no player character.** This gate does not pass on its own |

**Asking at a gate.** Say what you measured or need, give the options with a recommendation, and wait. Write the
answer in the state file.

### 0.4 The site folder

Everything specific to this machine lives in `site/`, which is git-ignored. Templates are in `site.example/`.

| File | Holds | Who writes it |
|---|---|---|
| `site/site.env` | Paths, database host and names, realm name and address, era, sizing | Agent, from G1 |
| `site/secrets.env` (mode 600) | `DB_PASS`, `DASHBOARD_TOKEN` (agent-generated, never shown); `RA_USER`, `RA_PASS` (operator, G4); optional `OPENROUTER_API_KEY` (operator) | Both |
| `site/fleet.toml` | Model backends, routes, lanes | Agent, from G5 |
| `site/SETUP-STATE.md` | Progress, gate answers, figures, open problems. **No secrets** | Agent, after every phase |

**State file layout:**

```markdown
# Setup state — <realm name>
| Phase | Status | Finished | Notes |
|---|---|---|---|
| 1 Survey | done | 2026-10-01 14:05 | 16 cores, 64 GB, 410 GB free |

## Gate answers
- G1: install root /opt/wow, run as `wow`, target 300 bots

## Figures
- Build: 52 min (nproc 16)

## Open problems
- (none)
```

**A new session reads `site/SETUP-STATE.md` first** and resumes at the first phase that isn't `done`. It re-runs that
phase's **Check** before trusting a `done` it didn't see.

### 0.5 Session setup

Your shell forgets variables between commands, so start **every** command with the environment:

```bash
. /opt/wow/headless-dm/ops/env.sh && <command>        # ops/env.sh
```

What `ops/env.sh` does, if you need to recreate it:

```bash
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SITE_DIR=${SITE_DIR:-$REPO/site}
set -a; . "$SITE_DIR/site.env"; [ -f "$SITE_DIR/secrets.env" ] && . "$SITE_DIR/secrets.env"; set +a
db() { MYSQL_PWD="$DB_PASS" mysql --default-character-set=utf8mb4 -h"$DB_HOST" -P"$DB_PORT" -u"$DB_USER" "$@"; }
ra() { "$REPO/ops/scripts/ra.sh" "$@"; }   # GM console; reads RA_USER / RA_PASS from the environment
```

- **Always go through `db`.** It passes `utf8mb4`: the `mysql` client otherwise defaults to latin1 and double-encodes
  accents. It also keeps the password off the command line.
- **`ra` is the GM console** on 127.0.0.1:3443, for example `ra "server info"`. The console serves **one session at a
  time**, so never run two `ra` calls at once. A client that didn't close blocks every later one; find it with
  `pgrep -a nc` and kill it by PID.

### 0.6 Long jobs

Builds, extraction and generation take minutes to hours. Run them detached and poll:

```bash
. ops/env.sh && mkdir -p "$LOGS_DIR/setup" && \
  nohup nice -n 10 <command> > "$LOGS_DIR/setup/<name>.log" 2>&1 < /dev/null & echo $! > "$LOGS_DIR/setup/<name>.pid"
# later:
tail -5 "$LOGS_DIR/setup/<name>.log"; kill -0 "$(cat "$LOGS_DIR/setup/<name>.pid")" && echo running || echo finished
```

- **`< /dev/null` is required,** or the job can hang waiting on input.
- **Never `pkill -f` a pattern that also matches your own shell command.** Kill by PID.
- **Record each job's wall time** under Figures. Future users plan model time from these.

---

## Phase 1 — Survey the machine

**Needs:** a shell on the server machine as a normal user.

**Steps.** Measure before recommending anything:

```bash
lsb_release -ds; uname -m; nproc
free -g | awk '/Mem:/{print $2" GB total, "$7" GB available"}'
df -h --output=avail,target / /opt 2>/dev/null
lspci 2>/dev/null | grep -Ei 'vga|3d|display' || echo "no GPU listed"
ss -ltn | grep -E ':(3306|3724|8085|11434|8787|3443)\b' || echo "game and service ports free"
systemctl is-active mysql 2>/dev/null || echo "no mysql service"
```

**Sizing.** Each continent updates on one thread, so bot count is bound by per-core speed more than core count. Subtract
the memory of any model servers on the same machine before reading this table.

| Available cores / RAM | `MapUpdate.Threads` | Bot target | MySQL buffer pool |
|---|---|---|---|
| 4–7 / 8–16 GB | 2 | 100 | 2 GB |
| 8–15 / 16–32 GB | 4 | 300 | 4 GB |
| 16+ / 32 GB+ | 4 | 500–850 | 8 GB |

- **Only the bottom row is measured.** The reference realm ran 700 bots at a 6 ms average world update (p95 15 ms) on
  32 cores. The other rows are estimates: grow in steps (Phase 12) and watch the update time.
- **Disk:**
  - source about 1 GB
  - build 2.3 GB
  - extracted data about 7.5 GB (mmaps 6 GB)
  - databases grow slowly (the reference dumps are 115 MB gzipped)
  - the client itself, if it lives here

**Gate G1.** Report the measurements and ask:
1. **Install root.** Recommend `/opt/wow`.
2. **Account the servers run as.** Recommend the operator's own account for a single-user machine, or a dedicated
   `wow` account.
3. **Bot target.** Recommend from the table. The first boot uses 50–100 whatever the target; Phase 12 grows it.

If the root is `/opt/wow`, the operator runs (G3):

```bash
sudo mkdir -p /opt/wow && sudo chown <account>: /opt/wow
```

**Check.**
- Ubuntu 24.04 x86_64.
- At least 25 GB free under the install root.
- The ports above are free. A running MySQL is fine; anything else on 3724, 8085 or 11434 must move first.
- `touch /opt/wow/.w && rm /opt/wow/.w` succeeds.

**Record.** The measurements, the G1 answers, and the recommended threads and buffer pool.

---

## Phase 2 — Source and site files

**Needs:** Phase 1; `git` available (`sudo apt install -y git` via G3 if not).

**Steps.**

```bash
git clone --recursive https://github.com/bazola/headless-dm.git /opt/wow/headless-dm   # or your own fork
cd /opt/wow/headless-dm
mkdir -p site && cp site.example/site.env site/site.env && cp site.example/fleet.toml site/fleet.toml
( umask 077; { printf 'DB_PASS=%s\n' "$(openssl rand -hex 16)"; printf 'DASHBOARD_TOKEN=%s\n' "$(openssl rand -hex 16)"; printf 'LORE_GATE_TOKEN=%s\n' "$(openssl rand -hex 16)"; } > site/secrets.env )
ops/bootstrap.sh        # links src/modules/* into the core, checks tools
```

**`bootstrap.sh` will report missing tools here, and that is expected.** It checks for `cmake`, `make`, `gcc`,
`mysql`, `nc` and `openssl`, none of which a fresh Ubuntu carries — Phase 3 installs them. The module links are
what matters now; run it again at the end of Phase 3 and every line should read `ok`. What it must *not* report
is an empty core checkout: that means the clone did not finish, and no later phase can recover from it.

What `bootstrap.sh` does to the modules, by hand:

```bash
for m in src/modules/*/; do n=$(basename "$m"); ln -sfn "../../modules/$n" "src/azerothcore-wotlk/modules/$n"; done
```

**Edit `site/site.env`** from G1:

| Key | Value |
|---|---|
| `WOW_ROOT` | the install root |
| `SERVER_PREFIX` | `$WOW_ROOT/server` |
| `DATA_DIR` | `$SERVER_PREFIX/data` |
| `LOGS_DIR` | `$SERVER_PREFIX/logs` |
| `BACKUP_DIR` | `$WOW_ROOT/backups` |
| `CLIENT_DIR` | set in Phase 6 |
| `WOW_USER` | the G1 account |
| `DB_HOST`, `DB_PORT`, `DB_USER` | `127.0.0.1`, `3306`, `acore` |
| `REALM_NAME` | ask the operator, or use "Living Azeroth" |
| `REALM_ADDRESS` | the address clients will use: LAN IP, VPN IP (e.g. Tailscale) or a public name; ask |
| `ERA` | `classic` |
| `REALM_FIRST_DAY` | today; the chronicle counts from it |
| `MAP_THREADS` | from Phase 1 |
| `BOT_MIN`, `BOT_MAX` | `50`, `100` for the first boot |
| `BOT_GUILDS` | `20`: bots found this many guilds, which become the companies |
| `DASHBOARD_BIND` | `127.0.0.1`; add the LAN or VPN address if the operator wants the dashboard from other machines |

**The checkout is not disposable.** The worldserver reads SQL from the source tree every time it starts (core updates,
module SQL, era brackets), and the path is baked in at build time. Don't move it after Phase 4.

**Gate G4.** Ask the operator to add two lines to `site/secrets.env` with an editor, for example
`nano /opt/wow/headless-dm/site/secrets.env`:
- `RA_USER=` a name for the GM console account the services use (not their player account).
- `RA_PASS=` its password. They will type it again at the first-boot console (Phase 7).
- Optionally `OPENROUTER_API_KEY=` for a remote model fallback (Phase 8).

**Check.**

```bash
git submodule status | grep -E '^[-+U]' && echo "SUBMODULES NOT CLEAN" || echo "submodules at pins"
test -f src/azerothcore-wotlk/CMakeLists.txt && echo "core checked out" || echo "CORE EMPTY"
for m in src/modules/*/; do [ -n "$(ls -A "$m")" ] || echo "EMPTY SUBMODULE: $m"; done
ls -l src/azerothcore-wotlk/modules/ | grep -c -- '->'          # 6
stat -c %a site/secrets.env                                      # 600
for k in DB_PASS DASHBOARD_TOKEN LORE_GATE_TOKEN RA_USER RA_PASS; do grep -q "^$k=." site/secrets.env && echo "$k set" || echo "$k MISSING"; done
. ops/env.sh && for k in WOW_ROOT REALM_ADDRESS ERA REALM_FIRST_DAY; do [ -n "${!k:-}" ] && echo "$k=${!k}" || echo "$k MISSING"; done
```

**Record.** The superproject commit (`git rev-parse HEAD`), `git submodule status` output, and the site.env values
except secrets.

---

## Phase 3 — Dependencies

**Needs:** Phase 2.

**Gate G3.** Give the operator these commands one at a time:

```bash
sudo apt update
sudo apt install -y cmake make gcc g++ clang mysql-server libmysqlclient-dev
sudo apt install -y libssl-dev libbz2-dev zlib1g-dev libreadline-dev libncurses-dev
sudo apt install -y libboost-filesystem-dev libboost-program-options-dev libboost-iostreams-dev
sudo apt install -y libboost-regex-dev libboost-thread-dev libboost-system-dev libboost-locale-dev
sudo apt install -y python3 python3-pil netcat-openbsd openssl curl
```

**Don't use `libboost-all-dev`.** On some machines it pulls in GPU compute libraries (ROCm), which you don't want next
to model servers.

**Check.**

```bash
cmake --version | head -1; g++ --version | head -1; mysql --version; python3 --version
python3 -c 'import sys, tomllib; assert sys.version_info >= (3, 11)' && echo "python ok"
nc -h 2>&1 | grep -q -- '-q' && echo "netcat-openbsd ok"
systemctl is-active mysql
```

Known to work: cmake 3.28, g++ 13.3, MySQL 8.0, Boost 1.83, Python 3.12.

**Record.** Versions.

---

## Phase 4 — Build

**Needs:** Phase 3.

**Steps.** A long job (0.6), 30–90 minutes:

```bash
. ops/env.sh && cd "$REPO/src/azerothcore-wotlk" && mkdir -p build && cd build && \
  cmake ../ -DCMAKE_INSTALL_PREFIX="$SERVER_PREFIX" -DCMAKE_BUILD_TYPE=Release \
    -DWITH_WARNINGS=0 -DSCRIPTS=static -DMODULES=static -DTOOLS_BUILD=all
# then, detached:
nohup nice -n 10 make -j"$(nproc)" install > "$LOGS_DIR/setup/build.log" 2>&1 < /dev/null &        # ops/build.sh
```

- **`-DTOOLS_BUILD=all`** builds the data extractors used in Phase 6.
- **`-DMODULES=static` is required.** mod-dashboard compiles against headers from mod-ollama-chat (cpp-httplib,
  nlohmann/json) and from mod-playerbots.
- **If the build breaks,** report the first error with its file and line (0.2 rule 5). The core fork and mod-playerbots
  move together; a pinned release should build.

**Check.**

```bash
. ops/env.sh && tail -3 "$LOGS_DIR/setup/build.log"
. ops/env.sh && grep -ciE "^(make(\[[0-9]+\])?: \*\*\*|.*: error:)" "$LOGS_DIR/setup/build.log"   # 0
ls "$SERVER_PREFIX/bin"        # authserver dbimport map_extractor mmaps_generator vmap4_assembler vmap4_extractor worldserver
ls "$SERVER_PREFIX/etc"/*.conf.dist "$SERVER_PREFIX/etc/modules"/*.conf.dist
```

The module confs must include `playerbots`, `mod_ollama_chat`, `mod_ledger`, `mod_dashboard` and `progression_system`.

**Record.** Build wall time and `nproc`.

---

## Phase 5 — Databases

**Needs:** Phase 3 (MySQL running).

**Steps.** Write the setup SQL to a private file. The password is expanded by the shell and never printed.

```bash
. ops/env.sh && mkdir -p "$WOW_ROOT/tmp" && ( umask 077; cat > "$WOW_ROOT/tmp/db-init.sql" <<SQL
CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS';
CREATE DATABASE IF NOT EXISTS acore_auth       DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS acore_characters DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS acore_world      DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS acore_playerbots DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL PRIVILEGES ON acore_auth.*       TO '$DB_USER'@'localhost';
GRANT ALL PRIVILEGES ON acore_characters.* TO '$DB_USER'@'localhost';
GRANT ALL PRIVILEGES ON acore_world.*      TO '$DB_USER'@'localhost';
GRANT ALL PRIVILEGES ON acore_playerbots.* TO '$DB_USER'@'localhost';
SQL
)        # ops/db-init.sh
```

**Gate G3.** The operator runs:

```bash
sudo mysql < /opt/wow/tmp/db-init.sql
```

Then delete the file: `rm /opt/wow/tmp/db-init.sql`.

- **Keep these four database names.** mod-playerbots needs the fourth, and module code checks `acore_characters`
  **by name**: `mod-ollama-chat/src/mod-ollama-chat_config.cpp:494` and `_personality.cpp:66` both query
  `information_schema` with the schema name written into the C++. Rename the character database and the module
  reports "Please source the required database table first" about a table that is sitting right there — measured
  on a scratch realm using `s5_characters`, where the table existed and the check still failed.

**Tuning** (above about 150 bots). Set `innodb_buffer_pool_size` in `ops/mysql/99-wow-tuning.cnf` to the Phase 1 value
(G3):

```bash
sudo cp /opt/wow/headless-dm/ops/mysql/99-wow-tuning.cnf /etc/mysql/mysql.conf.d/
sudo systemctl restart mysql
```

The file turns off the binary log and sets `innodb_flush_log_at_trx_commit = 2` and `READ-COMMITTED`. Bots write
constantly, and these settings trade about a second of durability on power loss for throughput. Restart MySQL only
while the worldserver is stopped.

**Check.**

```bash
. ops/env.sh && db -N -e "SELECT schema_name FROM information_schema.schemata
   WHERE schema_name IN ('$DB_AUTH','$DB_CHARACTERS','$DB_WORLD','$DB_PLAYERBOTS')"        # 4 rows
test ! -e "$WOW_ROOT/tmp/db-init.sql" && echo "init file removed" || echo "STILL THERE — it holds the password"
```

**Record.** Done; whether tuning was applied, and with what buffer pool.

---

## Phase 6 — Client data

**Needs:** Phase 4 (extractors built).

**Gate G2.** Ask the operator:
1. **Where is the client?** A 3.3.5a build 12340 enUS client. It can be copied into
   `/opt/wow/headless-dm/client/` (git-ignored) or left where it is.
2. **Is it build 12340 enUS?** The login screen shows the version in the lower left.
3. **Is it an HD repack?** Fine either way: only stock archives are extracted.

Set `CLIENT_DIR` in `site/site.env`.

**Steps.**

**6.1 A view of the stock archives.** The extractors expect exact file names (`Data/common.MPQ`,
`Data/enUS/locale-enUS.MPQ`). Many client copies are lowercase, and HD repacks add patches the server must not see.
Build a folder of links to exactly the stock archives:

```bash
. ops/env.sh && VIEW="$WOW_ROOT/extract/client" && mkdir -p "$VIEW/Data/enUS" && \
for f in common.MPQ common-2.MPQ expansion.MPQ lichking.MPQ patch.MPQ patch-2.MPQ patch-3.MPQ; do
  src=$(find "$CLIENT_DIR" -maxdepth 2 -ipath "*/data/$f" | head -1)
  [ -n "$src" ] && ln -sfn "$src" "$VIEW/Data/$f" || echo "MISSING $f"
done && \
for f in locale-enUS.MPQ expansion-locale-enUS.MPQ lichking-locale-enUS.MPQ patch-enUS.MPQ patch-enUS-2.MPQ patch-enUS-3.MPQ; do
  src=$(find "$CLIENT_DIR" -maxdepth 3 -ipath "*/data/enus/$f" | head -1)
  [ -n "$src" ] && ln -sfn "$src" "$VIEW/Data/enUS/$f" || echo "MISSING $f"
done        # ops/extract-data.sh does 6.1 and 6.2
```

**Any `MISSING` line stops the phase:** ask the operator about their client.

These archives are never linked, even when present:

| Archive | What it changes |
|---|---|
| `patch-6` | Terrain (spawns sit on stock terrain) |
| `patch-k` | Starting outfits |
| `patch-w` | Liquids |
| `patch-enus-a` | Character appearance rows |

**6.2 Extract.** A long job, about 10 minutes. Run each step from the data directory:

```bash
. ops/env.sh && mkdir -p "$DATA_DIR" && cd "$DATA_DIR" && B="$SERVER_PREFIX/bin" && \
  "$B/map_extractor" -i "$WOW_ROOT/extract/client" -o . && \
  "$B/vmap4_extractor" -d "$WOW_ROOT/extract/client/Data" && \
  mkdir -p vmaps && "$B/vmap4_assembler" Buildings vmaps && \
  "$B/mmaps_generator" --config "$B/mmaps-config.yaml" --threads "$(nproc)"
```

A few tiles dropped at the vertex limit during mmaps generation is normal. `Buildings/` is an intermediate; delete it
after the check.

**6.3 Dashboard map art** (recommended). This builds the dashboard's world map, which the era sweep in Appendix C also
uses:

```bash
. ops/env.sh && AC="$REPO/src/azerothcore-wotlk" && cd "$AC/modules/mod-dashboard/tools" && \
  cc -O2 -o mpq_extract mpq_extract.c -I"$AC/deps/libmpq" "$AC/build/deps/libmpq/libmpq.a" -lz -lbz2
# the extract_worldmap.py header gives the archive order for mpq_extract, then:
python3 extract_worldmap.py --out "$DATA_DIR/dashboard-maps"
```

**Check.**

```bash
. ops/env.sh && cd "$DATA_DIR" && for d in dbc maps vmaps mmaps; do printf '%s %s\n' "$d" "$(ls $d | wc -l)"; done
grep -m1 'Detected locale' map_extractor.log 2>/dev/null
ls dashboard-maps/manifest.json 2>/dev/null
```

Reference counts: `dbc` 246. `maps`, `vmaps` and `mmaps` should each hold thousands of files, and `mmaps_generator`
reports about 6,100 tiles.

**Record.** Client build and whether it is a repack (never its path contents), extraction times, file counts, and
whether map art was built.

---

## Phase 7 — Configure and first boot

**Needs:** Phases 5 and 6.

### 7.1 Apply the configuration

```bash
. ops/env.sh && "$REPO/ops/scripts/conf-apply.py"
```

What `conf-apply.py` does:
- Copies each missing `*.conf` from its `*.conf.dist`.
- Sets every key in `conf/*.overrides`, filling `${…}` values from `site/`.
- Backs up what it changes to `$BACKUP_DIR`.
- Refuses to write a literal placeholder.

**Count the configs before going on.** A fresh build installs only `*.conf.dist`, so every `.conf` here was
written by the command above; if it skipped one it says so under `not applied:` and **exits non-zero — that is a
failure, not a warning.** Phase 7.2 and 7.3 both read configs that must already exist:

```bash
. ops/env.sh && ls "$SERVER_PREFIX/etc"/*.conf "$SERVER_PREFIX/etc/modules"/*.conf | wc -l    # 9
```

Nine: `authserver`, `worldserver`, `dbimport`, and the six module confs. Anything less and the missing file's
server will start on `.dist` defaults — pointing at the wrong database, with the modules unconfigured.

**What the overrides turn on**, which you should be able to explain to the operator:

| File | Keys |
|---|---|
| worldserver | DB connections, `DataDir`, `LogsDir`, `MapUpdate.Threads = ${MAP_THREADS}`, `Console.Enable = 0`, `Ra.Enable = 1` + `Ra.IP = "127.0.0.1"` (GM console, port 3443), `MaxPlayerLevel = 60`, `CharacterCreating.Disabled.RaceMask = 1536` (no blood elves or draenei yet). `Expansion` stays 2: lowering it breaks character loading |
| playerbots | `PlayerbotsDatabaseInfo`, `MinRandomBots = ${BOT_MIN}`, `MaxRandomBots = ${BOT_MAX}`, `RandomBotGuildCount = ${BOT_GUILDS}`, era (`EraExpansion = 0`, `RandomBotMaxLevel = 60`, `RandomBotMaps = 0,1`, `DisableDeathKnightLogin = 1`), canned chatter off (`RandomBotTalk = 0`, `EnableBroadcasts = 0`; `AIPlayerbot.GuildFeedback = 0` keeps upstream's own capital-"AI" typo and is **inert in this module version** — the key appears only in the `.conf.dist`, never in a `GetOption` call), level brackets, company behaviour (`Company*`) |
| mod_ollama_chat | `Url` = the router, `Model = ambient`, `MaxConcurrentQueries`, `Roleplay.Enable = 1` + `Strictness = 2`, `EnableRPPersonalities = 1`, `EnableRAG = 1`, `Regard.Enable = 1`, `Regard.CompanyWords = 1`, `Chronicle.Rumours = 1`, `Relationship.Enable = 0` and `EnableSentimentTracking = 0` (regard replaces both), `SkipMasterCommands = 1`, the in-world prompt templates |
| mod_ledger | `RecordBotChat = 1`, `RecordGuildBots = 1` |
| mod_dashboard | `Bind = ${DASHBOARD_BIND}` (must include 127.0.0.1), `Port = ${DASHBOARD_PORT}`, `CommandToken = ${DASHBOARD_TOKEN}`, `MapRoot = ${DATA_DIR}/dashboard-maps`, `DataRoot` |
| progression_system | `Bracket_0` … `Bracket_60_1_2 = 1` (Classic through Molten Core), `ReapplyUpdates = 0` |

### 7.2 Import the databases

```bash
. ops/env.sh && cd "$SERVER_PREFIX/bin" && ./dbimport > "$LOGS_DIR/setup/dbimport.log" 2>&1 < /dev/null; echo "exit $?"
```

### 7.3 First boot (Gate G6)

The first boot is the only one with an interactive console: the GM accounts have to be created before the services can
use the remote console. The operator runs it in their own terminal, because they type passwords there.

Give the operator these steps:
1. **Terminal 1:** `cd /opt/wow/server/bin && ./authserver`
2. **Terminal 2:** `cd /opt/wow/server/bin && AC_CONSOLE_ENABLE=1 ./worldserver`
   - The environment variable overrides `Console.Enable` for this run only.
   - The first start is slow: era brackets apply, then bot accounts and characters are created.
3. **Wait** until you (the agent) see `ready...` in `$LOGS_DIR/Server.log`, then tell them to type at the `AC>` prompt:
   ```
   account create <RA_USER> <RA_PASS>
   account set gmlevel <RA_USER> 3 -1
   account create <their player account> <their password>
   ```
   Use the values they put in `site/secrets.env` for the first two lines.
4. **Leave it running about 10 minutes** so bots randomise and found their guilds, then type `server shutdown 1`.
   Stop the authserver with Ctrl-C.

### 7.4 Realm row and era SQL

```bash
. ops/env.sh && db acore_auth -e "UPDATE realmlist SET name='$REALM_NAME', address='$REALM_ADDRESS' WHERE id=1;"
. ops/env.sh && db "$DB_AUTH" < "$REPO/sql/era/classic.sql"   # holds every account at the era's expansion
```

`classic.sql` sets every account to expansion 0. Player accounts created later need expansion 0 too: run it again, or
set it when creating them.

### 7.5 Run under systemd (Gate G3)

The operator runs:

```bash
sudo /opt/wow/headless-dm/ops/systemd/install.sh        # renders units for WOW_USER and SERVER_PREFIX
sudo systemctl start wow-auth wow-world
```

**Check.**

```bash
. ops/env.sh && systemctl is-active wow-auth wow-world
grep -c 'ready\.\.\.' "$LOGS_DIR/Server.log"
grep -m1 '\[Ollama Chat\] Config loaded' "$LOGS_DIR/Server.log" | cut -c1-120
db -N -e "SELECT COUNT(*) FROM acore_characters.updates WHERE state='MODULE'; SELECT COUNT(*) FROM acore_world.updates WHERE state='MODULE';"
db -N acore_characters -e "SHOW TABLES LIKE 'ledger\_%'; SELECT COUNT(*) FROM guild;"
db -N acore_auth -e "SELECT a.username, aa.gmlevel FROM account a JOIN account_access aa ON aa.id=a.id;"
ra "server info"
```

- `ready...` appears.
- The module config line is present.
- The characters and world `updates` tables hold MODULE rows between them.
- `ledger_chat` and `ledger_event` exist.
- The `RA_USER` account has gmlevel 3.
- `server info` answers and, after a few minutes, shows bots in the world.

**Record.** First-boot duration, guild count after the first boot, and bots online.

---

## Phase 8 — Models and router

**Needs:** Phase 7.

**Gate G5.** Ask, in this order:
1. **Where can models run?**
   - A GPU in this machine.
   - Another machine on the network.
   - Several machines.
   - A gaming PC that is also used to play.
   - Nowhere local, only a remote API.
2. **For each machine:**
   - Its address.
   - The server software: llama.cpp `llama-server`, Ollama, LM Studio, or another OpenAI-compatible server.
   - The model loaded, or the VRAM free if nothing is running yet.
   - How many requests it can take at once without hurting other use.
3. **A remote API fallback?** If yes, the operator adds `OPENROUTER_API_KEY` to `site/secrets.env`.

**If nothing is running yet,** recommend a model by the VRAM free. These are rough fits for a quantized instruct model
with room for 4 slots of context:

| VRAM free | Model |
|---|---|
| 8–12 GB | Qwen3 8B, Q4–Q6 |
| 16–24 GB | Qwen3 14B, Q4–Q6 |
| 24 GB+ | Qwen3 30B-A3B (MoE), Q4 |

Start llama-server with thinking off:

```bash
llama-server -m <model>.gguf --host 0.0.0.0 --port 8080 --jinja -np 4 -c 32768 \
  --chat-template-kwargs '{"enable_thinking":false}'
```

**Per-server notes:**
- **llama-server:** `-c` is the total context shared across the `-np` slots, so 32768 over 4 slots gives 8k each.
  Writing jobs need about 8k per slot.
- **Ollama:** its default port 11434 belongs to the router. Move it with `OLLAMA_HOST=0.0.0.0:11435`.
- **LM Studio:** it ignores `chat_template_kwargs`. Use `system_suffix = "/no_think"` for Qwen3, and the exact model
  id.

### 8.1 Probe every backend

For each backend, test with a real request. `/health` can answer "ok" on a wedged server.

```bash
URL=http://<host>:<port>; MODEL=<exact id>
curl -s -m 120 -w '\n%{time_total}s\n' "$URL/v1/chat/completions" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"In one short sentence, greet a traveller on the road.\"}],\"max_tokens\":80}"
curl -s -m 120 "$URL/v1/chat/completions" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"temperature\":0,\"response_format\":{\"type\":\"json_object\"},\"messages\":[{\"role\":\"user\",\"content\":\"Reply with a JSON object with key ok set to true.\"}],\"max_tokens\":40}" \
  | python3 -c 'import sys,json; c=json.load(sys.stdin)["choices"][0]["message"]["content"]; print("json ok" if json.loads(c.split("</think>")[-1]).get("ok") is True else "json BAD")'
```

A backend passes when:
- the greeting has text and no reasoning in `content`,
- the JSON test prints `json ok`,
- the time is recorded.

A backend that fails either test is left out, or gets `system_suffix`, and is probed again.

### 8.2 Write `site/fleet.toml`

**`site.example/fleet.toml` is the authority for this file's shape** — Phase 2 already copied it to
`site/fleet.toml`. Edit that copy rather than typing one from scratch; the schema below is what
`services/common/fleet_config.py` actually reads.

```toml
[router]
bind       = "127.0.0.1"
port       = 11434
queue_wait = 20       # seconds a request waits on a busy backend before failing over

[backends.main]
url            = "http://127.0.0.1:8080"
model          = "qwen3-14b"
router_slots   = 4    # live-chat requests in flight: latency-bound
batch_slots    = 2    # batch generations in flight: throughput-bound. Two numbers, on purpose
router_timeout = 90   # someone is waiting for this line
batch_timeout  = 300  # a backstory is not a chat message
kinds          = ["guild", "character", "rag", "traits"]
# lmstudio = true     # LM Studio rejects json_object and needs json_schema
# system_suffix = "/no_think"

[routes]              # live traffic via the router: first backend with a free slot wins, failover in order
ambient   = ["main"]  # bot chatter and replies: lowest latency first
quality   = ["main"]
chronicle = ["main"]
prose     = ["main"]
utility   = ["main"]
moment    = ["main"]  # add a remote backend here to use a paid fallback

[batch]               # which machines batch lore generation may use, in preference order
lanes = ["main"]

[judge]               # JSON verdicts at temperature 0
backend = "main"
slots   = 3
timeout = 60
```

**`batch_slots` is not optional, and omitting it fails silently.** A backend without it defaults to **0**, which
drops it out of `[batch]` altogether; the lane list then comes back empty, the worker pool starts zero threads,
and every batch lore command in Phase 10 reports that it finished having generated nothing. There is no error to
read. If Phase 10 produces no rows, check this first.

**Assigning several backends:**
- **Fastest** first in `ambient`.
- **Largest** first in `[batch] lanes`, and in `chronicle` and `prose`.
- **Smallest one that passed the JSON test** for `[judge]`.
- **A gaming PC** goes last in every route, with at most 2 `router_slots`, so it only takes work when the others
  are busy.

**Slot budget.** Live chat, the judge and batch writing can run at the same time, and each backend is described
**once** — one entry per machine, not per route, or its caps are duplicated and stop meaning anything. Keep a
machine's `router_slots` + `batch_slots` + its judge slots within what it can actually serve. Set
`CHAT_CONCURRENCY` in `site/site.env` (it becomes `OllamaChat.MaxConcurrentQueries`) to the sum of the `ambient`
backends' `router_slots`.

**For comparison, the reference realm:**
- Live chat went to a 35B MoE with an 80B fallback.
- Writing and chronicles ran on 80B and 31B models on a second machine.
- The judge was an 8B next to the game server.
- A gaming PC was listed last.

### 8.3 Start the router

```bash
. ops/env.sh && "$REPO/ops/scripts/conf-apply.py"        # picks up CHAT_CONCURRENCY
```

**Gate G3.** The operator runs:

```bash
sudo systemctl start wow-router
sudo systemctl restart wow-world
```

The router, `services/router/03-router.py`, listens on `:11434` and speaks the Ollama API to mod-ollama-chat. The route
name arrives as the model name.
- **Translation:** Ollama requests become OpenAI calls. `num_predict` becomes `max_tokens`; JSON format becomes
  `response_format`.
- **Thinking:** `<think>` blocks are stripped, and `/api/show` reports only `completion`, so the module never turns
  thinking on.
- **Failover:** a backend busy for 20 s, or failing, passes the request to the next one in the route.

**Check.**

```bash
curl -s localhost:11434/api/tags | python3 -c 'import sys,json; print([m["name"] for m in json.load(sys.stdin)["models"]])'
curl -s -m 120 localhost:11434/api/generate -d '{"model":"ambient","prompt":"Say good morning to a farmer.","stream":false}' \
  | python3 -c 'import sys,json; r=json.load(sys.stdin)["response"]; print(r); assert r.strip() and "<think>" not in r'
. ops/env.sh && ra "ollama status" "ollama test hello"
```

- The routes are listed.
- The generate call returns clean text.
- `ollama status` shows the endpoint healthy. If `ollama test`'s reply isn't in the console output, look for it in
  `Server.log`.

If replies are slow or missing, read the router log first (G3: `sudo journalctl -u wow-router -n 50`). It prints the
backend and latency for every request.

**Record.** Each backend: address, software, model, slots, greeting time, JSON pass or fail. The route and lane
assignment.

---

## Phase 9 — First contact

**Needs:** Phase 8.

**Gate G8.** Give the operator these steps:
1. **Point the client at the realm.** Set `Data/enUS/realmlist.wtf` (the lowercase repack path is
   `data/enus/realmlist.wtf`) to `set realmlist <REALM_ADDRESS>`.
2. **Remote players only:** open TCP 3724 and 8085, or use a private network such as Tailscale.
3. **Create a character** of a Classic race: no blood elf, draenei or death knight.
4. **Find a bot** near the starting area and `/say` hello to it.
5. **Report** what it said, and whether it sounded like a person in the world rather than a player.

Bots have no backstories yet, so their voices are generic. That is expected until Phase 10.

**While they play:**

```bash
# Run this BEFORE the operator says anything and write both numbers down. The realm has been talking to
# itself since Phase 7, so these tables are already large; what proves this phase is the difference.
. ops/env.sh && db -N acore_characters -e "SELECT COUNT(*) FROM ledger_chat; SELECT COUNT(*) FROM ledger_event;"
# ...then run the same line again once they report back.
```

Both counts rise **above the figures you took before G8**. A single count taken afterwards proves nothing here:
neither table is empty by this phase, whatever the bot did or didn't say.

**Optional client addon:** [CleanBot](https://github.com/bennybroseph/CleanBot) (MIT) gives a UI for commanding your
own bots. Commands to your own bots don't get spoken replies (`SkipMasterCommands`).

**Check.**
- The operator reports an answer in character.
- The ledger counts rise **above the pre-G8 figures**.

**Record.** The operator's words about the reply, and any lag they saw.

---

## Phase 10 — Lore

**Needs:** Phase 9. Bots have been online long enough to found their guilds.

Every generation step:
- **Resumes** if interrupted.
- **Writes rejections** to `review-*.jsonl` next to the script.
- **Runs as a long job** (0.6). Record each wall time.

`ops/seed-world.sh` runs Phases 10 and 11 in order and stops at each check.

### 10.1 Freeze the companies

```bash
. ops/env.sh && db -N acore_characters -e "SELECT COUNT(*) FROM guild;"
```

Continue when the count reaches `BOT_GUILDS`, or hasn't risen in 30 minutes. Then:
1. Set `BOT_GUILDS=0` in `site/site.env`.
2. Run `conf-apply.py`.
3. The operator restarts the worldserver (G3): `sudo systemctl restart wow-world`.

Stories and seats assume rosters stay put. A guild bot re-rolled after the freeze loses its tabard; that is a known
side effect.

### 10.2 Knowledge base

mod-ollama-chat ships reference JSON (classes, zones, dungeons) written for players. Rewrite it in the voice of people
who live there, then trim it to the era:

```bash
. ops/env.sh && cd "$REPO/services/lore" && python3 rag_inworld.py inworld --route chronicle --workers 2
. ops/env.sh && cd "$REPO/services/lore" && python3 rag_inworld.py revise --era "$ERA" --backup-dir "$BACKUP_DIR/rag-$(date +%F)"
```

Entries about later eras move from `$DATA_DIR/ollama-rag/` to `_parked-classic.json.txt`; they come back at the matching
era. The knowledge base loads at worldserver start.

### 10.3 Temperaments

```bash
. ops/env.sh && db acore_characters < "$REPO/sql/characters/temperaments-inworld.sql"
```

This loads the in-world temperaments. Every backstory draws its character's temperament from them, so load them before
10.4.

### 10.4 Backstories, traits, standing

```bash
. ops/env.sh && cd "$REPO/services/lore" && python3 gen_backstories.py sample --era "$ERA"
```

**Read the sample yourself first.** Stop and report to the operator if anything in it breaks Appendix A: game words,
numbers, later-era names or player talk.

Then, in order, each as a long job:

```bash
python3 gen_backstories.py generate --era "$ERA" --unguilded   # company histories, their members, then everyone else
python3 gen_backstories.py traits --era "$ERA"                 # personality, gist, motivation, standing level
python3 gen_backstories.py crafts --era "$ERA"                 # a trade and a household for everyone who lacks one
python3 gen_backstories.py traits-report --era "$ERA"          # read it: repeated names, openings, temperaments
python3 gen_backstories.py restand --era "$ERA" --dry-run
python3 gen_backstories.py restand --era "$ERA"
python3 gen_backstories.py era-scan --era "$ERA"               # later-age names in stored texts; exit 1 on a hit
python3 gen_backstories.py project
```

Then reload the personalities: `. ops/env.sh && ra "ollama reload"`.

**What each step does:**
- **`generate`** skips characters above the era's level cap. It checks each text by pattern and then by the judge.
- **`traits`** derives a personality, a gist and a motivation from each story. It also stamps the level the story was
  written for.
- **`crafts`** gives everyone a trade and a household, which every prompt then carries. Skip it and the cast has
  backstories but no daily working life to speak from.
- **`restand`** revises a story that is two level bands behind its character, keeping names, places and events. It
  acts on evidence only (the ledger's level-ups).
- **`project`** writes what the worldserver reads:
  - `BIO_<guid>`: personality, gist and motivation, for chatter and events.
  - `BIOX_<guid>`: personality, the whole backstory and motivation, for when someone speaks to the bot.
  - Each bot's assignment, guild info and MOTD from the company's charter and motto, and the dashboard's `lore.json`.

**Repeated names.** If `traits-report` shows one people's names repeating, rewrite that people's name pool and rename
the characters who used the old one:

```bash
python3 gen_backstories.py name-pools --era "$ERA" --cultures Troll
python3 gen_backstories.py repool --era "$ERA" --old <previous names.json> --cultures Troll
```

**Fix, don't regenerate.** If `era-scan` finds a company history naming a later age, fix the history by hand. Its
members' stories were written from it.

**Check.**

```bash
. ops/env.sh && db -N acore_characters -e "
  SELECT COUNT(*) FROM guild;
  SELECT COUNT(*) FROM lore_guild WHERE era='$ERA';
  SELECT COUNT(*) FROM lore_character WHERE era='$ERA';
  SELECT COUNT(*) FROM lore_character WHERE era='$ERA' AND personality IS NOT NULL;
  SELECT COUNT(*) FROM mod_ollama_chat_personality_templates WHERE \`key\` LIKE 'BIO\_%';
  SELECT COUNT(*) FROM mod_ollama_chat_personality_templates WHERE \`key\` LIKE 'BIOX\_%';"
```

- Histories match the guild count, except guilds whose members are all parked by the era.
- Every character at or below the cap has a story and traits.
- The `BIO_` and `BIOX_` counts equal the character count.
- `era-scan` reports nothing, **or every hit it reports is read and judged.** It sweeps each stored text with the
  era's own rejection pattern and exits 1 if anything matched, so it can gate the phase — but a pattern that names
  places in plain words does produce false positives (a character trusted "with her blade's edge" is talking about
  a sword, not Outland). Read each hit before fixing it: real ones go to `restand` or a regeneration, company
  histories are fixed by hand, and a term that keeps firing on innocent prose belongs bound tighter in `era.py`.

**Record.** Wall time and model per step, counts, and the traits-report summary.

### 10.5 Name the main — G10

**Needs:** 10.4 done, and the lore gate running (Phase 11 installs its unit; start it by hand here if it is not up).

Every account has one character it calls its own; the rest are that person's alts. Nothing infers this — until an
account names its main, its characters get no bond, no personal chronicle, and the lore gate serves them nothing.
**The failure is silent.** Nothing on screen explains the emptiness, so a realm set up without this looks like one
where the chronicler and the lore gate are broken. That is why this is a gate and not a suggestion.

**Steps.** The token lives in `site/secrets.env` as `LORE_GATE_TOKEN` (W15), so `ops/env.sh` already exported it:

```bash
. ops/env.sh && GATE="http://${LORE_GATE_BIND:-127.0.0.1}:${LORE_GATE_PORT:-8788}"
curl -s -H "X-Lore-Token: $LORE_GATE_TOKEN" "$GATE/claim"    # accounts that have named no main
curl -s -X POST "$GATE/main-set" -H "X-Lore-Token: $LORE_GATE_TOKEN" \
  -H 'Content-Type: application/json' -d '{"character": "<name>"}'
```

`/main-set` takes the character's **name**. The gate resolves it against the same list `/claim` returns and answers
404 for anything not on it, so there are no ids to look up. Without a token it answers *"The gate has no token set,
so it stays shut."* (503) or *"That token is not this gate's."* (401) — it never serves an unauthenticated request.

**`/claim` answers only while nobody on the realm has named a main.** It is guarded that way because holding the
token does not prove you own an account. So the browser route works for the *first* account and no other: a second
person's main must be set with `LORE_GATE_ACCOUNTS=<id>` on the gate, or with the SQL below. The same SQL is the
fallback if you would rather not go through the gate at all:

```bash
. ops/env.sh && db acore_characters -e \
  "INSERT INTO player_main (account_id, main_guid) VALUES (<id>, <guid>)
   ON DUPLICATE KEY UPDATE main_guid = VALUES(main_guid);"
```

Then write the alts' bonds and stories — this is the step that turns the account's other characters into people who
know the main:

```bash
. ops/env.sh && cd "$REPO/services/lore" && python3 gen_backstories.py alts --era "$ERA"
```

**Gate — G10.** Ask the operator: *which character do you play as yourself?* Then either name it, or write down that
they chose to skip it.

- **A main is named** — the ordinary answer, and the one every personal feature depends on.
- **Skipped, deliberately** — a realm of bots with no player character is a real thing to want, and it stays
  possible. Record the words "no main by choice" in the state file so no later session treats the empty table as a
  mistake and re-asks.

Do not pass this gate by assuming either answer, and do not warn-and-continue: the whole point is that the failure it
guards is invisible from inside the game.

**Check.**

```bash
. ops/env.sh && db -N acore_characters -e "
  SELECT COUNT(*) FROM player_main;
  SELECT COUNT(*) FROM lore_alt;"
```

- **If a main was named:** at least one row, and a bond for each of that account's other characters. If `alts` says
  *"no alts found"*, the main was never actually set — go back to the POST.
- **If the operator skipped:** `player_main` is empty and that is correct. The check is that the state file says so.

**Record.** Which character was named and for which account, or that it was skipped and by whose decision. Note any
further accounts still to be named — each one needs this step again.

### 10.6 The almanac of places — optional, and skippable

**Needs:** 10.4 done (same fleet lanes), and **a realm that has actually been played.** The zone list comes from the
realm's own kill record, so a realm booted an hour ago has nothing to describe yet and this step should simply be
skipped until it has. Come back to it after a few days.

This writes one short fragment per zone per angle — what a traveller notices, what kills people there, who lives
there, what they complain about — so a companion in Duskwood does not sound like one in Mulgore.

**Nothing about Azeroth is in this repository, and this step does not put any there.** The zones come from
`ledger_event`, the names from the client's own `AreaTable.dbc`, and every generated sentence goes into the
`place_words` table in your database. Two realms produce two different almanacs and neither one is committed.

```bash
. ops/env.sh && cd "$REPO/services/lore"
python3 gen_places.py report --era "$ERA"                      # how many zones are worth describing yet
python3 gen_places.py sample --era "$ERA" --variants 1         # prints four kinds of fragment; stores nothing
```

**Read the sample yourself first.** Stop and report to the operator if anything in it breaks Appendix A: game words,
numbers, later-era names, or advice aimed at the reader. The last one matters more than it looks — a fragment that
says *"watch the shadows"* would install that habit in every zone at once, so the generator rejects it, and a sample
full of them means the lane is ignoring the prompt.

Then, as a long job (§0.6):

```bash
python3 gen_places.py generate --era "$ERA"                    # every usable zone, four angles each
python3 gen_places.py collide  --era "$ERA"                    # phrases shared between zones
```

**Check.** `report` shows most usable zones described, and `collide` reports few or no 4-grams shared across zones.
If `collide` lists many, run it with `--prune` and `generate` again — a phrase repeated in every zone is worse than
no almanac at all, and it is invisible to any test that only asks whether bots mention their zone more.

**Nothing reads this table yet.** The prompt section that would use it is not built, and when it is, it ships
switched off behind its own config key. So generating the almanac early is safe, and skipping it costs nothing.

**Record.** How many zones were described and how many fragments, or that it was skipped and why (usually: too
little play history yet).

---

## Phase 11 — Companies, society, chronicle, the era's world

**Needs:** Phase 10.

### 11.1 Seats, relations, ranks

```bash
. ops/env.sh && cd "$REPO/services/regard" && python3 regard.py once        # creates the regard and company tables
. ops/env.sh && cd "$REPO/services/regard" && python3 rivalry.py seed --era "$ERA"
. ops/env.sh && cd "$REPO/services/regard" && python3 rivalry.py ranks --era "$ERA"
```

- **`rivalry.py seed`** seats companies per level band, then sets relations between them. If it reports seats
  incomplete, run it again.
- **`rivalry.py ranks`** writes in-world rank names, which show after a restart.

### 11.2 Bot society

```bash
. ops/env.sh && cd "$REPO/services/regard" && python3 society.py fellowship    # company members know each other
. ops/env.sh && cd "$REPO/services/regard" && python3 society.py lore          # people named in backstories become feelings
```

### 11.3 Start the services (Gate G3)

```bash
. ops/env.sh && "$REPO/ops/systemd/install.sh" --user        # installs wow-regard and wow-chronicler
```

The operator runs:

```bash
sudo loginctl enable-linger <WOW_USER>
```

Then start them:

```bash
systemctl --user enable --now wow-regard wow-chronicler
```

**What regard does every 120 s:**
1. Reads new ledger rows, and who stands near whom from the dashboard's `/bots`.
2. Has the judge weigh each moment, and writes each side's feeling in words from its own memory.
3. Decays old feelings.
4. Reckons company influence and land.
5. Runs membership: candidacy, officer invites, loyalty, recruiting, defections.
6. Lets bots talk among themselves while no real player is online.

**What the chronicler does every 600 s:** it writes each closed four-hour watch for both factions, then turns the
entries into rumours. Bots repeat the rumours in the zones where they are told.

### 11.4 The era's world (Gates G9, G3)

Ask the operator to approve the Classic court overlay. It puts Bolvar and Prestor in Stormwind Keep, parks the
later-age figures, and moves their quests to the new holders. The revert file undoes it.

```bash
. ops/env.sh && mkdir -p "$BACKUP_DIR" && db acore_world -e "SELECT 1" && \
  MYSQL_PWD="$DB_PASS" mysqldump --default-character-set=utf8mb4 -h"$DB_HOST" -u"$DB_USER" acore_world \
  | gzip > "$BACKUP_DIR/world-before-classic-court-$(date +%F).sql.gz"
. ops/env.sh && db "$DB_WORLD" < "$REPO/sql/world/classic-court.sql"
```

The operator restarts the worldserver (G3): `sudo systemctl restart wow-world`.

**Check** (the chronicle rows need up to four hours for the first closed watch):

```bash
. ops/env.sh && db -N acore_characters -e "
  SELECT COUNT(*) FROM guild_seat; SELECT COUNT(*) FROM guild_relation; SELECT COUNT(*) FROM company_rank_name;
  SELECT COUNT(*) FROM regard; SELECT COUNT(*) FROM regard_log WHERE ts > NOW() - INTERVAL 30 MINUTE;
  SELECT COUNT(*) FROM chronicle_entry; SELECT COUNT(*) FROM chronicle_rumour;"
systemctl --user is-active wow-regard wow-chronicler
curl -s "http://127.0.0.1:8787/health"
```

- Seats, relations and rank names are above zero.
- `regard` is above zero, and `regard_log` gains rows within 30 minutes of play.
- Both units are active, and the dashboard answers.
- Within four hours, `chronicle_entry` and `chronicle_rumour` are above zero.

The operator can open the dashboard at `http://<DASHBOARD_BIND>:8787/` and see the Feelings, Companies and Chronicle
panels fill.

**Record.** The seat and relation counts, the first watch written, and whether the court overlay was applied.

---

### 11.5 The merchants' stalls — optional

The auction house is stocked by merchants: a service account nobody logs into, holding characters with in-world
names. Skip this and the houses stay empty; nothing else depends on it.

**Needs:** 11.4 done, so the era's brackets and `disables` are applied. The item list is built from them.

**Steps.**

1. Name the merchants in `site/site.env`. `MERCHANT_ACCOUNT` is an account name that must not already exist;
   `MERCHANT_CHARACTERS` is `Name:race:class:gender` per merchant, comma-separated. The race decides the house,
   so give both factions merchants if you want both houses stocked. The file's own comment lists the race codes
   and the reference realm's ten.

   **The module creates the account and the characters itself** at startup, through the core's guid generator.
   Never insert them with SQL while the server is running: a guid created before the next restart collides.

2. Build the era's item list. The module only filters by level and ID range, which lets later-era goods through,
   so this writes the list of what the era's open world actually yields:

```bash
. ops/env.sh && python3 "$REPO/services/economy/era_items.py" --era "$ERA"
```

   It writes `$DATA_DIR/economy/allowed-items.txt` plus a `.report` beside it. The item IDs come from
   `acore_world`, which is Blizzard-derived: **the output stays on this machine.**

3. Apply the configuration and restart the worldserver (G3):

```bash
. ops/env.sh && "$REPO/ops/scripts/conf-apply.py"
```

**Check.**

```bash
. ops/env.sh
wc -l "$DATA_DIR/economy/allowed-items.txt"
db -N -e "USE $DB_AUTH; SELECT COUNT(*) FROM account WHERE username = '$MERCHANT_ACCOUNT';"
db -N -e "USE $DB_CHARACTERS; SELECT COUNT(*) FROM characters c JOIN $DB_AUTH.account a ON a.id = c.account
          WHERE a.username = '$MERCHANT_ACCOUNT';"
db -N -e "USE $DB_CHARACTERS; SELECT COUNT(*) FROM auctionhouse;"
```

- The item list has thousands of lines, not zero.
- The account exists, and holds one character per `MERCHANT_CHARACTERS` entry.
- `auctionhouse` climbs over the first hour.

**If the houses stay empty,** the usual cause is the allow-list: a path the worldserver cannot read **stops the
seller silently**, with the merchants created and nothing listed. Check the path in the live
`modules/mod_ahbot.conf` resolves, and that the file is readable by the account the server runs as.

**Record.** The item count, the merchant names created, and the first hour's listing count.

---

## Phase 12 — Scale to the target

**Needs:** Phase 11. The realm has been stable for an hour.

`add-bots.sh` does one step up:
1. Raises `MinRandomBots` and `MaxRandomBots`.
2. Restarts the worldserver, which needs sudo.
3. Waits for logins and a level-bracket pass.
4. Runs `lore-fill.sh` for the new bots.
5. Reports.

**Steps.**

```bash
. ops/env.sh && "$REPO/ops/scripts/add-bots.sh" --count 100 --dry-run
```

**Gate G7.** Show the dry run and confirm the step. The operator runs the real step in their own terminal, because it
asks for sudo (G3):

```bash
/opt/wow/headless-dm/ops/scripts/add-bots.sh --count 100
```

`add-bots.sh` records the new `BOT_MIN`/`BOT_MAX` in `site/site.env`. After it finishes:

```bash
. ops/env.sh && "$REPO/ops/scripts/add-bots.sh" --report && ra "server info"
```

Repeat until the target is reached, one step at a time.

**Check.**
- Bots online approach `BOT_MAX`.
- The mean update time in `server info` stays under 50 ms.
- New bots have stories (the report's lore coverage).
- If the online count plateaus below target, see Appendix D.

**Record.** For each step: bots online, update time mean and p95, and lore fill time.

---

## Phase 13 — Hand over

**Needs:** Phase 12.

### 13.1 Backups (Gate G3)

```bash
. ops/env.sh && "$REPO/ops/scripts/backup.sh"        # dumps four databases + site/ + live confs into BACKUP_DIR, rotates
```

`install.sh --user` renders `wow-backup.service` and `wow-backup.timer` into the **user** manager
(`~/.config/systemd/user`) — they are never installed system-wide, so `sudo systemctl enable` cannot find them.
As `WOW_USER`, not as root:

```bash
systemctl --user enable --now wow-backup.timer      # nightly
```

The one part that does need root is lingering, without which the user manager stops when they log out and the
timer never fires: `sudo loginctl enable-linger <WOW_USER>`.

Tell the operator plainly:
- Backups stay on this machine in `BACKUP_DIR`, and include their secrets.
- Copying that folder to an external drive is their job.
- A dead disk takes local backups with it.
- Keep the client folder and the model files with the archive.

### 13.2 Summary for the operator

Write the final section of `site/SETUP-STATE.md` and tell the operator:
- **Dashboard URL.** The command token is in `site/secrets.env`.
- **Daily commands and kill switches** (Appendix B).
- **What loads live and what needs a restart** (Appendix B).
- **The era and what the next phase would be** (Appendix C).
- **The figures from this setup:** build, extraction, each generation job, and bots per update time.
- **Open problems.**

**If they keep their own private repo** for this realm, commit `site/site.env` and `site/fleet.toml` there. **Never
commit `site/secrets.env`.**

**Check.**
- A backup file from today exists in `BACKUP_DIR`.
- `systemctl --user list-timers | grep wow-backup` shows the next run. **`--user` matters:** the system-scope
  command finds nothing however well the timer is configured, because the unit only exists in the user manager.
- Every phase in the state file is `done`.

---

## Appendix A — Architecture and design rules

```
 game client ──► authserver :3724
      │
      └────────► worldserver :8085 ────────────────────────► MySQL
                  ├ mod-playerbots     bots, companies, era gate    acore_auth
                  ├ mod-ollama-chat    LLM speech ──► router ──►    acore_characters
                  ├ mod-ledger         chat & deeds, append-only    acore_world
                  ├ mod-dashboard      :8787 map, panels, pause     acore_playerbots
                  └ mod-progression-system  era brackets
                                                   │
                        router :11434 ─────────────┴──► model servers (site/fleet.toml)

 services (Python, stdlib) ── read the ledger, write tables the modules reload:
   lore        backstories, temperaments, motivations → BIO_/BIOX_ personality templates
   regard      feelings in words, companies, membership, bot society (every 120 s)
   chronicler  faction scribes per four-hour watch → rumours (every 600 s)
```

### Rules the design keeps

- **Bots are people living in Azeroth, never players.** Nothing a bot is told uses game vocabulary or numbers: "badly
  hurt", not "23% health"; "a task you took on", not "quest". Roleplay is always on.
- **The world's clock is the current era.** In Classic, nothing later has happened. When the era advances, characters
  gain a chapter; they are not rewritten.
- **Game objects are touched only on the world thread.** Workers do model calls and string work, and results come back
  through per-tick queues.
- **Every model-driven behaviour has a chance gate and a kill switch, and defaults to off.** A build with defaults
  behaves like stock.
- **The core stays unmodified.** Changes live in modules, as labelled commits on each fork's `custom-wow` branch.
- **Backends can leave at any time.** Routes fail over, queues wait, nothing crashes.
- **Blizzard-derived data stays local:** the client, extracted data, map art and world database dumps.

### Repositories

| Path | Repository | Branch | Changes |
|---|---|---|---|
| `src/azerothcore-wotlk` | azerothcore-wotlk (Playerbot fork) | `Playerbot`, pinned | none |
| `src/modules/mod-playerbots` | mod-playerbots | `custom-wow` | ledger hooks, era gate, company standing, regard gate, company actions, defection |
| `src/modules/mod-ollama-chat` | mod-ollama-chat | `custom-wow` | ledger hook, roleplay words, regard, lore tiers, company words, rumours, quest words, master orders |
| `src/modules/mod-progression-system` | mod-progression-system | pinned | none |
| `src/modules/mod-ledger` | mod-ledger | `main` | original |
| `src/modules/mod-dashboard` | mod-dashboard | `main` | original |

Changing any of it: `docs/EXTENDING.md`.

---

## Appendix B — Operating

| Task | How |
|---|---|
| Add bots | `ops/scripts/add-bots.sh --count 100` (operator; sudo); `--dry-run`, `--resume`, `--report` |
| Lore for new or re-rolled bots | `ops/scripts/lore-fill.sh` (generate, traits, restand, project, `ollama reload`) |
| Record a conf change | `ops/scripts/conf-overrides.py`, then commit `conf/` in your own repo |
| Apply confs | `ops/scripts/conf-apply.py` |
| Pause a bot | Dashboard, or `POST /cmd/pause {"guid":N}` with header `X-Dashboard-Token`. It freezes in place, still fights back, and the pause ends at logout |
| Services | `systemctl --user status wow-regard wow-chronicler` |
| Tune chat | Edit `mod_ollama_chat.conf`, then `ra "ollama reload"` |
| Health | `ra "server info"`, `ra "ollama status"`, router journal |
| Rebuild after a code change | `nice -n 19 make -j6 install` in `build/`; safe while running, takes effect at restart. New source files or modules need `cmake .` first |

### Kill switches

| Switch | Stops |
|---|---|
| `services/regard/PAUSE` | The whole regard cycle |
| `services/regard/NO_CANDIDACY` | Candidacy and regard asides |
| `services/regard/NO_COMPANY_ACTIONS` | Invites, promotions, removals; withdraws pending rows |
| `services/regard/NO_FOUNDING` | Stories for player-founded companies |
| `services/regard/NO_TALK` | Bot-to-bot talk |
| `services/chronicler/PAUSE` | The chronicle |
| `AiPlayerbot.Company*Chance = 0`, `CompanyRegardGate = 0`, `CompanyActions = 0` | Company behaviour in game (restart) |
| `OllamaChat.Regard.Enable = 0`, `Chronicle.Rumours = 0` | Feelings and rumours in prompts (`ollama reload`) |

### Reload or restart

| Change | Takes effect |
|---|---|
| `mod_ollama_chat.conf`, personalities (`project`) | `ollama reload` |
| Regard rows, rumours, company standing | Periodic reloads, within a minute or two |
| Any other conf, guild info, rank names, knowledge base, world SQL | Worldserver restart |
| C++ | Rebuild, `make install`, restart |

**Never use `.playerbots rndbot reload`.** It is unsafe; restart instead.

---

## Appendix C — Eras

**Dump `acore_world` before every step.** Bracket SQL is one-way, and the dump is the only undo. Every step is a G9
gate.

**Classic phases.** Enable the next `ProgressionSystem.Bracket_*` key and restart. The order: Molten Core, then Onyxia,
then Blackwing Lair, then Zul'Gurub, then the Ahn'Qiraj war effort and AQ20, then AQ40. When Onyxia opens, re-point
quests 4184 and 4185 to Bolvar (1748).

**Checking the world for the era.** `ops/scripts/era-spawn-sweep.py --zone "<zone>"` lists spawns that don't belong
to the era. It needs the map art from Phase 6.3.

**The Burning Crusade.** One restart once the code exists:

| Area | Change |
|---|---|
| Accounts | `account.expansion = 1` |
| Level caps | `MaxPlayerLevel = 70`, `RandomBotMaxLevel = 70` |
| Bots | `RandomBotMaps = 0,1,530`, `EraExpansion = 1` |
| Character creation | `CharacterCreating.Disabled.RaceMask = 0` |
| Game rules | Restore attunements; remove the arena disables |
| Brackets | Enable brackets 61_64 through 70_2_2 |
| Knowledge base | Restore the parked entries |

The code isn't written yet: a `tbc` era, Outland lands, Outland geography for the chronicler, and a "new chapter" pass
(`docs/EXTENDING.md` §6).

---

## Appendix D — Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Bots run into walls or fall through floors | Data doesn't match the client build, or HD patches were extracted. Redo Phase 6 from the stock view |
| No bot replies | `ra "ollama status"`; the router journal; probe the backend with a real request (8.1) |
| Replies include reasoning | Thinking is on. Turn it off on the server, or set `system_suffix = "/no_think"` (LM Studio) |
| Garbled accents in lore | A `mysql` call without `utf8mb4`. Use `db` |
| `ra` hangs or is refused | Another console session is open. `pgrep -a nc`, kill by PID |
| Later-era names in stories | `gen_backstories.py era-scan`. By hand, use `REGEXP BINARY`: plain `REGEXP` ignores case, so "Dead Scar" matches "dead Scarlet". Fix company histories by hand |
| Presence and bot talk do nothing | The dashboard isn't reachable at `127.0.0.1:8787` |
| Online bots plateau below `MaxRandomBots` | Account sizing assumes 9 loginable characters per account, but the era gate parks some classes and races. Raise `BOT_MAX` further |
| Worldserver stops at once under systemd | `Console.Enable = 1` with no terminal. Keep it 0 |
| Characters of later races fail to load | `Expansion` was lowered. Keep it 2; the era gate does the rest |
| A backend "healthy" but silent | llama-server's `/health` can lie on a wedged server. Probe with a real request; restart it |
| First boot's `AC>` prompt never appears | It was started under systemd, or without `AC_CONSOLE_ENABLE=1`. Stop it and redo 7.3 in a terminal |
| A bot won't ride the Deeprun Tram | **Known limitation, not a fault.** Trams are deliberately excluded from the boarding code ("ignore static transports"), and the travel graph that would walk one has its only call site commented out; upstream's own comments say transports are not properly handled yet. Boats and zeppelins *do* work. Bots walk the tunnel or are summoned. Decided 2026-09-18 not to fix (plan 25 §32) |
| A bot walks somewhere a flight path would reach | Expected today: ordinary travel never chooses to fly, though `taxi` works when asked and bots mirror a flight you take. Open work, not a limitation (plan 25 item 73) |
| A sentence you typed drew no reply at all | **Known limitation: orders and speech share a vocabulary.** `OllamaChat.BlacklistCommands` — **211 entries live, 192 unique**, more than the `.conf.dist` shows — drops any message *beginning* with one of them, from anyone, on any channel. **66 are plain English** (`follow`, `stay`, `help`, `trade`, `open`, `accept`, `loot`, `quest`, `talk`, `wait`, `look`, `move`, `pull`, `roll`…) and 18 are one or two characters (`s`, `t`, `e`, `u`, `b`, `go`, `do`…). The match is case-sensitive, so "follow me" is dropped and "Follow me" is not, and the dropped line never reaches `ledger_chat` either — a session review shows only a gap where the answer should be. Separately, `SkipMasterCommands` takes a *master's* line on whisper/party/raid/guild as an order when it matches a real playerbots command name. **Workaround: name the bot first** ("Grommell, follow me") or capitalise the opening word — both are answered normally. Decided 2026-09-18 to document, not fix (plan 25 §39) |

---

## Appendix E — Licenses and credits

| Code | License |
|---|---|
| This repository (services, ops, docs, SQL), mod-ledger, mod-dashboard | MIT |
| Our mod-ollama-chat changes | AGPL-3.0, like the upstream module |
| Our mod-playerbots changes | GPL-2.0-or-later, like the upstream module |
| AzerothCore, mod-progression-system | Unchanged, under their own licenses |

- **The server binary as shipped is AGPL-3.0.** It links GPL-2.0-or-later code with AGPL-3.0 code; the MIT modules can
  be linked in, but the binary as a whole is AGPL.
- **Running a modified server** for other people means offering them its source. The public forks do that.
- **Third-party notices** are in `NOTICE`.

**Built on:**
- [AzerothCore](https://www.azerothcore.org/)
- [mod-playerbots](https://github.com/mod-playerbots/mod-playerbots)
- [mod-ollama-chat](https://github.com/DustinHendrickson/mod-ollama-chat)
- [mod-progression-system](https://github.com/azerothcore/mod-progression-system)
- [llama.cpp](https://github.com/ggml-org/llama.cpp)

World of Warcraft and Warcraft are trademarks of Blizzard Entertainment. This project is not affiliated with or
endorsed by Blizzard, and distributes no Blizzard data.
