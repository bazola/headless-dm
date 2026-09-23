"""The fleet, read from site/fleet.toml instead of written into the code (plan 23 W3).

Two callers with different needs read the same file:

  * the **router** wants routes — ordered failover lists of backends, with live-chat timeouts and caps;
  * **batch lore** wants lanes — the same machines, with batch caps and longer timeouts.

They are the same servers described twice, which is exactly why they were drifting: `fleet.py` and
`03-router.py` each carried their own copy of six addresses, and the judge lane was constructed in three
separate files. One file, asked by role.

A backend carries two caps and two timeouts on purpose. The Z13 has eight physical slots; live chat may
use four and batch takes three, so neither ever takes all eight. One number could not say that.

Falls back to the values that were hardcoded before this existed, so a realm with no fleet.toml still
runs — the same way site.py falls back to worldserver.conf.
"""

import os
import sys

try:
    import tomllib                      # 3.11+
except ModuleNotFoundError:             # pragma: no cover
    tomllib = None

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from common import site  # noqa: E402

# What the code assumed before there was a file to ask. Kept so nothing breaks on a realm that has not
# written one; a single local llama-server is the profile the clean-room setup is tested against.
_FALLBACK = {
    "router": {"bind": "0.0.0.0", "port": 11434, "queue_wait": 20},
    "backends": {
        "local": {"url": "http://127.0.0.1:8080", "model": "local", "router_slots": 4,
                  "batch_slots": 2, "router_timeout": 90, "batch_timeout": 300,
                  "kinds": ["guild", "character", "rag", "traits"]},
    },
    "routes": {k: ["local"] for k in ("ambient", "quality", "chronicle", "prose", "utility", "moment")},
    "batch": {"lanes": ["local"]},
    "judge": {"backend": "local", "slots": 3, "timeout": 60},
}


def path():
    return os.environ.get("FLEET_TOML") or os.path.join(site.SITE_DIR, "fleet.toml")


def load(strict=False):
    """The whole file as a dict. `strict` refuses the fallback, for callers that would rather stop."""
    p = path()
    if not os.path.exists(p):
        if strict:
            sys.exit(f"no {p} — copy site.example/fleet.toml and describe your model servers")
        return _FALLBACK
    if tomllib is None:
        sys.exit("python 3.11+ is needed to read fleet.toml (tomllib)")
    with open(p, "rb") as fh:
        data = tomllib.load(fh)
    for section in ("backends", "routes"):
        if not data.get(section):
            sys.exit(f"{p} has no [{section}] — see site.example/fleet.toml")
    return data


def backend(name, cfg=None):
    """One backend's settings, with the defaults filled in. Unknown name is fatal: a typo in a route
    should say so here rather than become a silent hole in the failover list."""
    cfg = cfg or load()
    got = (cfg.get("backends") or {}).get(name)
    if got is None:
        known = ", ".join(sorted((cfg.get("backends") or {})))
        sys.exit(f"fleet.toml names no backend {name!r}; it has: {known}")
    out = dict(got)
    out.setdefault("model", "local")
    out.setdefault("router_slots", 4)
    out.setdefault("batch_slots", 0)
    out.setdefault("router_timeout", out.get("timeout", 60))
    out.setdefault("batch_timeout", out.get("timeout", 300))
    out.setdefault("kinds", [])
    out.setdefault("lmstudio", False)
    out.setdefault("system_suffix", None)
    out["name"] = name
    # A paid backend without its key is not an error: it drops out of every route it appears in, which
    # is what "optional fallback" has to mean for a realm that never bought one.
    key_env = out.get("api_key_env")
    out["api_key"] = os.environ.get(key_env, "") if key_env else ""
    out["usable"] = bool(out["api_key"]) if key_env else True
    return out


def routes(cfg=None):
    """{route name: [backend dicts]}, with unusable paid backends already dropped."""
    cfg = cfg or load()
    out = {}
    for name, names in (cfg.get("routes") or {}).items():
        chain = [backend(n, cfg) for n in names]
        out[name] = [b for b in chain if b["usable"]]
    return out


def batch_lanes(cfg=None):
    """The backends batch lore may use, in preference order, minus any with no batch slots."""
    cfg = cfg or load()
    names = (cfg.get("batch") or {}).get("lanes") or list(cfg.get("backends") or {})
    return [b for b in (backend(n, cfg) for n in names) if b["usable"] and b["batch_slots"] > 0]


def judge(cfg=None):
    """The judge backend plus its own slots and timeout. One definition, not three."""
    cfg = cfg or load()
    j = dict((cfg.get("judge") or {}))
    b = backend(j.get("backend") or "local", cfg)
    b["slots"] = int(j.get("slots", 3))
    b["router_timeout"] = b["batch_timeout"] = int(j.get("timeout", 60))
    return b


def router(cfg=None):
    cfg = cfg or load()
    r = dict(_FALLBACK["router"])
    r.update(cfg.get("router") or {})
    return r
