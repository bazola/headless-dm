# Headless DM

An AzerothCore 3.3.5a realm where playerbots are voiced by language models as people who live in Azeroth. They have
backstories, feelings, companies and rumours, and the world follows a Classic → TBC → WotLK release schedule. The
server modules are submodules under `src/`. The Python services are in `services/`, and ops scripts are in `ops/`.

## Setting up

- **Follow `docs/GUIDE.md` phase by phase.** Read its Part 0 and Appendix A first.
- **Start from the state file.** Read `site/SETUP-STATE.md` if it exists, and resume at the first phase not marked
  done, after re-running the previous phase's Check.
- **Stop at every gate (G1–G10)** and ask the operator. Don't start a phase until the previous Check passes.
  G10 (Phase 10.5, naming the main) does not pass on its own: either a character is named, or the operator says
  outright that this realm has no player character.

## Changing things

Read `docs/EXTENDING.md` first. The architecture, and the rules the design keeps, are Appendix A of
`docs/GUIDE.md`.

## Rules

- **Bots are people living in Azeroth, never players.** Nothing a bot reads uses game vocabulary or numbers.
  Roleplay is always on.
- **The world's clock is the era in `site/site.env`.** Nothing later has happened.
- **Game objects only on the world thread.** Every model-driven behaviour has a chance gate and a kill switch, and
  defaults to off.
- **No core diffs.** Module changes are commits on the fork's `custom-wow` branch.
- **Never run `sudo`.** Give the operator one short command at a time.
- **Secrets live only in `site/secrets.env`.** Never print them; use `ops/env.sh`'s `db` and `ra` helpers.
- **Never copy, commit or upload client files, extracted data, map art or world database dumps.**
- **Back up before one-way steps** (bracket SQL, world overlays, conf changes), and say where the backup is.
- **On a failure, report the exact error and stop.** Don't work around something the guide doesn't cover.
- **The GM console serves one session at a time.** Never run two `ra` calls at once.
