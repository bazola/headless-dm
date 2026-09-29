#!/usr/bin/env python3
"""Remove bot memories that are the prompt handed back, not anything that happened (plan 58).

    python3 services/memory/scrub_notes.py                      dry run: counts and a sample
    python3 services/memory/scrub_notes.py --apply              delete them
    python3 services/memory/scrub_notes.py --also-conf <file>   add an older conf's prompts to the scaffolding

The same two tests the module now applies before storing a note (mod-ollama-chat_memory.cpp,
DropScaffoldNotes / EndsCutOff):

  * an echo -- a note sharing a run of four words (two of them four letters or longer) with the memory
    prompts or the system prompts: "No army has sailed for Northrend", "One per line. Prefix each ...";
  * the era prompt paraphrased -- Northrend, "dead and closed", "beyond this time" (FRAMING below);
  * a cut-off -- a note with no closing mark whose last word cannot end a sentence: "The Scourge held the".

The module also checks that the words were not in what the model was given; the prompts are gone by now,
so this cannot. Measured over 42,714 notes before first use, every sampled drop was scaffolding.

Deleting is safe with the worldserver running: the module saves insert-only, so a deleted row is never
written back, and it keeps its own copy in RAM until the next restart reloads the table. The AFTER DELETE
trigger copies every removed row into mod_ollama_chat_memories_archive, so nothing is lost for good.
"""
import argparse
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from recall import MODULE_CONF, log, sql  # noqa: E402

# The module's default for OllamaChat.Memory.SystemPrompt, used when the conf does not set one.
MEMORY_SYSTEM_DEFAULT = ("You keep short notes of what really happened. Write only what the lines you are given "
                         "show, never anything they do not, and follow the requested format exactly.")
PROMPT_KEYS = ("OllamaChat.SystemPrompt", "OllamaChat.Memory.SystemPrompt", "OllamaChat.Memory.EventPrompt",
               "OllamaChat.Memory.CondensePrompt")
DANGLING = set("the a an of to and or but in on at by for from with into onto his her their its my your our as "
               "that which who whose than was were is had has".split())
# The system prompt's three claims about the era, as the models paraphrased them. Exact runs of words miss
# "no army sailed for Northrend" and "the Dark Portal lay dead and closed"; before plan 58 no memory writer
# could have met Northrend in this era except through that prompt, so every mention of it is the prompt.
FRAMING = re.compile(r"northrend|dead and closed|beyond this time", re.I)
CLOSERS = ('.', '!', '?', '"', "'", ')', '”', '’', '…')


def words(text):
    return re.findall(r"[a-z0-9'\u0080-\U0010ffff]+", text.lower())


def shingles(text):
    w = words(text)
    return {" ".join(w[i:i + 4]) for i in range(len(w) - 3) if sum(len(x) >= 4 for x in w[i:i + 4]) >= 2}


def conf_prompts(path):
    out = []
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        sys.exit(f"cannot read {path}")
    for key in PROMPT_KEYS:
        m = re.search(r"^" + re.escape(key) + r'\s*=\s*"(.*)"\s*$', text, re.M)
        if m:
            out.append(m.group(1).replace("\\n", "\n"))
    return out


def scaffolding(paths):
    prompts = [MEMORY_SYSTEM_DEFAULT]
    for p in paths:
        prompts += conf_prompts(p)
    out = set()
    for p in prompts:
        for piece in re.split(r"\{[^}]*\}", p):   # placeholders are what was given, not what was asked
            out |= shingles(piece)
    return out


def verdict(text, scaffold):
    t = text.rstrip()
    w = words(t)
    if w and w[-1] in DANGLING and not t.endswith(CLOSERS):
        return "cut off"
    if shingles(t) & scaffold:
        return "echoes the prompt"
    if FRAMING.search(t):
        return "the era prompt, paraphrased"
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    ap.add_argument("--also-conf", action="append", default=[], help="an older mod_ollama_chat.conf to include")
    ap.add_argument("--show", type=int, default=20)
    args = ap.parse_args()

    scaffold = scaffolding([MODULE_CONF] + args.also_conf)
    rows = sql("SELECT id, bot_guid, REPLACE(REPLACE(memory_text, '\\n', ' '), '\\t', ' ') "
               "FROM mod_ollama_chat_memories")
    hits = [(r[0], r[1], r[2], why) for r in rows if (why := verdict(r[2], scaffold))]
    by = {}
    for h in hits:
        by[h[3]] = by.get(h[3], 0) + 1
    log(f"{len(rows)} memories, {len(hits)} to remove ({by}) across {len({h[1] for h in hits})} bots")
    random.seed(0)
    for _, bot, text, why in random.sample(hits, min(args.show, len(hits))):
        print(f"  [{why}] {bot}: {text[:140]}")

    if not args.apply:
        log("dry run; nothing deleted (--apply to delete)")
        return
    ids = [h[0] for h in hits]
    for i in range(0, len(ids), 500):
        sql("DELETE FROM mod_ollama_chat_memories WHERE id IN (" + ",".join(ids[i:i + 500]) + ")", fetch=False)
    log(f"deleted {len(ids)}; the archive trigger kept a copy of each")


if __name__ == "__main__":
    main()
