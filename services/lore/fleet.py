#!/usr/bin/env python3
"""Fleet lanes for batch lore work.

Batch jobs call each backend directly (OpenAI chat API), not through the router:
the router's per-backend timeouts are sized for live chat and cut long
generations off, and a route only ever uses its first free backend. Here every
model available works at once, each lane capped so live chat keeps room.

**Which machines those are is not written here.** They come from site/fleet.toml
(plan 23 W3), which is also what the router reads -- the two used to carry
separate copies of the same six addresses, and copies drift. A lane's batch
slots are deliberately a different number from its router slots: a box with
eight slots might give four to chat and three to batch, so neither takes all of
it. Each lane also declares which kinds of work it may take, because a slow
machine should carry no guild histories -- one thousand-token JSON would hold up
that whole roster.

Pool runs one worker thread per lane slot over a shared priority queue. A job is
fn(lane, attempt); a failed job is requeued for a different lane when one can
take it, up to MAX_ATTEMPTS. A lane with 3 connection failures in a row rests
for 60 s (backends can leave the pool at any time; nothing crashes).
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

# realpath, not abspath: this is reached through /opt/wow/lore, a symlink into the repo, and abspath
# does not follow one -- it would look for `common` under /opt/wow, where there is none (plan 23 W1).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from common import fleet_config as _fleet  # noqa: E402

MAX_ATTEMPTS = 4
THINK_RE = re.compile(r"<think>.*?</think>\s*", re.S)


class Lane:
    def __init__(self, name, url, model, slots, kinds, timeout=300, system_suffix=None, lmstudio=False):
        self.name, self.url, self.model = name, url, model
        self.slots, self.kinds, self.timeout = slots, set(kinds), timeout
        self.system_suffix = system_suffix
        self.lmstudio = lmstudio   # LM Studio takes response_format json_schema or text, never json_object (HTTP 400)
        self.lock = threading.Lock()
        self.fail_streak = 0
        self.down_until = 0.0
        self.done = self.failed = self.tokens = 0
        self.seconds = 0.0

    def available(self):
        return time.time() >= self.down_until

    def chat(self, messages, max_tokens, temperature=0.8, json_mode=False):
        if self.system_suffix:
            messages = [{"role": "system", "content": self.system_suffix}] + list(messages)
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens,
                "temperature": temperature, "stream": False}
        if json_mode:
            body["response_format"] = ({"type": "json_schema", "json_schema": {"name": "reply", "schema": {"type": "object"}}}
                                       if self.lmstudio else {"type": "json_object"})
        req = urllib.request.Request(self.url + "/v1/chat/completions", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                out = json.load(r)
        except (urllib.error.URLError, OSError) as e:  # includes timeouts and HTTP errors
            with self.lock:
                self.fail_streak += 1
                if self.fail_streak >= 3:
                    self.down_until = time.time() + 60
                    self.fail_streak = 0
            raise RuntimeError(f"{self.name} backend: {e}") from None
        text = THINK_RE.sub("", out["choices"][0]["message"].get("content") or "").strip()
        with self.lock:
            self.fail_streak = 0
            self.tokens += (out.get("usage") or {}).get("completion_tokens") or 0
            self.seconds += time.time() - t0
        return text


def _from_config(b):
    """One backend entry from site/fleet.toml as a Lane. Batch caps and batch timeouts, not the
    router's: a backstory is not a chat message, and the two want very different numbers."""
    return Lane(b["name"], b["url"], b["model"], b["batch_slots"], b["kinds"],
                timeout=b["batch_timeout"], system_suffix=b["system_suffix"], lmstudio=b["lmstudio"])


def all_lanes():
    """Every backend that can take batch work, by name. Addresses come from site/fleet.toml (plan 23
    W3) -- they used to be written out here, and again in the router, and the two drifted.

    Which lanes may take which kinds is a property of the machine, not of this file: a slow one should
    carry no "guild", because a thousand-token guild history would hold up that whole roster.
    """
    cfg = _fleet.load()
    return {name: _from_config(_fleet.backend(name, cfg))
            for name, b in (cfg.get("backends") or {}).items()
            if _fleet.backend(name, cfg)["batch_slots"] > 0}


def default_lane_names():
    return [b["name"] for b in _fleet.batch_lanes()]


DEFAULT_LANES = ""   # kept for callers that still pass it; empty means "whatever fleet.toml prefers"


def pick_lanes(names=None, slots=None):
    """names: 'a,b'; slots: 'lane=n,lane=n' overrides (0 drops the lane)."""
    lanes = all_lanes()
    wanted = [n.strip() for n in (names or "").split(",") if n.strip()] or default_lane_names()
    missing = [n for n in wanted if n not in lanes]
    if missing:
        raise SystemExit(f"fleet.toml has no batch lane named {', '.join(missing)}; "
                         f"it offers: {', '.join(sorted(lanes))}")
    chosen = [lanes[n] for n in wanted]
    for item in filter(None, (slots or "").split(",")):
        name, n = item.split("=")
        for lane in chosen:
            if lane.name == name.strip():
                lane.slots = int(n)
    return [lane for lane in chosen if lane.slots > 0]


_lane_fallback_warned = set()


def prefer_lanes(names=None, slots=None):
    """pick_lanes, but a lane name this fleet.toml does not have is a PREFERENCE, not a fatal error.

    A lane name written into our code ("evo-quality,z13-qwen35") is this realm's hardware, not the
    project's. Another realm names its machines differently, and `pick_lanes` raises SystemExit on a
    name it cannot find -- which `except Exception` does NOT catch, because SystemExit is a
    BaseException. An always-on service (regard, the chronicler) therefore exits mid-cycle and systemd
    restart-loops it every 60s forever, taking everything later in the cycle with it.

    So: use the named lanes when they exist, and otherwise fall back to whatever this fleet.toml
    prefers, saying so once. On a realm that HAS the named lanes this is exactly pick_lanes.
    """
    lanes = all_lanes()
    wanted = [n.strip() for n in (names or "").split(",") if n.strip()]
    missing = [n for n in wanted if n not in lanes]
    if missing:
        key = ",".join(sorted(missing))
        if key not in _lane_fallback_warned:
            _lane_fallback_warned.add(key)
            print(f"fleet: no batch lane named {', '.join(missing)}; using this fleet's own lanes "
                  f"({', '.join(default_lane_names()) or 'none'}) instead", file=sys.stderr, flush=True)
        return pick_lanes("", slots)
    return pick_lanes(names, slots)


class Judge:
    """Asks qwen3-8b whether a text is out of its era. Fast (~2 s) and a second
    opinion only: when the lane is down, check() returns None and the caller
    relies on its regex."""

    def __init__(self, slots=None):   # None means "whatever fleet.toml says", not a hardcoded 3
        # One definition of the judge, in site/fleet.toml. It was written out here and again in
        # regard.py and society.py, three copies of one address that could disagree (plan 23 W3).
        j = _fleet.judge()
        # Resolve slots ONCE, here: the semaphore below takes the same number, and passing None down
        # to BoundedSemaphore is a TypeError rather than a default.
        slots = int(slots or j["slots"])
        self.lane = Lane(j["name"], j["url"], j["model"], slots, set(),
                         timeout=j["router_timeout"], lmstudio=j["lmstudio"])
        self.sem = threading.BoundedSemaphore(slots)
        self.warned = False

    def check(self, prompt):
        """Evidence string when the text is flagged, else None."""
        if not self.lane.available():
            return None
        with self.sem:
            try:
                out = self.lane.chat([{"role": "user", "content": prompt}], 150, temperature=0.0, json_mode=True)
            except Exception as e:
                if not self.warned:
                    print(f"judge unavailable ({e}); outputs pass on the regex alone", flush=True)
                    self.warned = True
                return None
        m = re.search(r"\{.*\}", out, re.S)
        try:
            verdict = json.loads(m.group(0)) if m else {}
        except ValueError:
            return None
        if verdict.get("out_of_time") is True:
            return (str(verdict.get("evidence") or "") or "no evidence given").strip()[:200]
        return None


class Pool:
    def __init__(self, lanes, log=print):
        self.lanes, self.log = lanes, log
        self.cv = threading.Condition()
        self.jobs, self.failures = [], []
        self.pending = self.seq = 0

    def submit(self, kind, prio, fn, label):
        with self.cv:
            self.seq += 1
            self.jobs.append(dict(kind=kind, prio=prio, seq=self.seq, fn=fn, label=label,
                                  attempts=0, last_lane=None, errors=[]))
            self.pending += 1
            self.cv.notify_all()

    def _take(self, lane):
        best = None
        for j in self.jobs:
            if j["kind"] not in lane.kinds:
                continue
            if j["last_lane"] == lane.name and any(
                    other is not lane and j["kind"] in other.kinds and other.available() for other in self.lanes):
                continue
            if best is None or (j["prio"], j["seq"]) < (best["prio"], best["seq"]):
                best = j
        if best:
            self.jobs.remove(best)
        return best

    def _worker(self, lane):
        while True:
            with self.cv:
                while True:
                    if self.pending == 0:
                        return
                    j = self._take(lane) if lane.available() else None
                    if j:
                        break
                    self.cv.wait(timeout=5)
            j["attempts"] += 1
            t0 = time.time()
            try:
                result = j["fn"](lane, j["attempts"])
            except Exception as e:
                with lane.lock:
                    lane.failed += 1
                j["errors"].append(f"{lane.name}: {e}")
                with self.cv:
                    if j["attempts"] >= MAX_ATTEMPTS:
                        self.failures.append(j)
                        self.pending -= 1
                        self.log(f"[{lane.name}] FAILED {j['label']}: {e}")
                    else:
                        j["last_lane"] = lane.name
                        self.jobs.append(j)
                        self.log(f"[{lane.name}] retry {j['label']} (attempt {j['attempts']}): {e}")
                    self.cv.notify_all()
                continue
            with lane.lock:
                lane.done += 1
            with self.cv:
                self.pending -= 1
                left = self.pending
                self.cv.notify_all()
            self.log(f"[{lane.name}] ok {j['label']}{' ' + str(result) if result else ''} "
                     f"({time.time() - t0:.0f}s, {left} left)")

    def run(self):
        threads = [threading.Thread(target=self._worker, args=(lane,), daemon=True)
                   for lane in self.lanes for _ in range(lane.slots)]
        t0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.log(f"pool finished in {time.time() - t0:.0f}s")
        for lane in self.lanes:
            rate = f"{lane.tokens / lane.seconds:.1f} tok/s per request" if lane.seconds and lane.tokens else "-"
            self.log(f"  {lane.name:12} slots {lane.slots}  ok {lane.done:4}  failed {lane.failed:3}  {rate}")
        return self.failures
