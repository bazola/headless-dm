"""Where this realm lives: one loader for every service and script (plan 23 W1).

Values come from `site/site.env` and `site/secrets.env`, shell-style `KEY=value` files, so the *same two
files* are read here and `source`d by `ops/env.sh` and the shell scripts. Nothing in this module is
specific to a machine; those two files are the only thing that is.

**It falls back to the live server config.** Until a realm writes `site.env`, every database key is read
out of `worldserver.conf` exactly as the services did before, so W1 can land one service at a time instead
of as a flag day. The fallback searches the module confs too, because `PlayerbotsDatabaseInfo` is empty in
`worldserver.conf` on a realm that keeps it in `modules/playerbots.conf`.

Secrets are returned, never printed, and never written anywhere by this module.

Layout, resolved without any absolute path baked in:

    <repo>/services/common/site.py   ->  ROOT = <repo>
    $SITE_DIR, else <repo>/site      ->  site.env, secrets.env
"""

import os
import sys

# realpath, not abspath: the services are reached through /opt/wow symlinks, and abspath does not follow
# one -- ROOT would become /opt and site/ would be looked for in the wrong place.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
SITE_DIR = os.environ.get("SITE_DIR") or os.path.join(ROOT, "site")

# The four schemas, by the name the services use for them.
DB_KEYS = {"auth": "LoginDatabaseInfo", "world": "WorldDatabaseInfo",
           "characters": "CharacterDatabaseInfo", "playerbots": "PlayerbotsDatabaseInfo"}


def _parse(path):
    """A shell-style KEY=value file, as `source` would see it, minus expansion."""
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                key, sep, value = line.partition("=")
                if not sep:
                    continue
                value = value.strip()
                # Trailing comments only where the value is not quoted; a password may contain a '#'.
                # A quoted value may still be FOLLOWED by a comment -- `TOKEN="abc" # from openssl`.
                # Testing only the last character misses that and leaves the quotes in the value,
                # while ops/env.sh, sourcing the same file in bash, strips them. The two readers have
                # to agree on every line: that agreement is the whole reason this module exists.
                if value[:1] in ("'", '"'):
                    quote = value[0]
                    close = value.rfind(quote)
                    trailer = value[close + 1:].strip() if close > 0 else None
                    if close > 0 and (trailer == "" or trailer.startswith("#")):
                        value = value[1:close]
                elif " #" in value:
                    value = value.split(" #", 1)[0].strip()
                out[key.strip()] = value
    except OSError:
        return {}
    return out


def _load():
    values = {}
    values.update(_parse(os.path.join(SITE_DIR, "site.env")))
    values.update(_parse(os.path.join(SITE_DIR, "secrets.env")))
    # A real environment variable wins, so one-off overrides and systemd units keep working.
    for key in list(values):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


_VALUES = _load()


def reload():
    """Re-read both files. For a long-running service told its site config changed."""
    global _VALUES
    _VALUES = _load()
    return _VALUES


def get(key, default=None):
    value = _VALUES.get(key, os.environ.get(key, default))
    return default if value in (None, "") else value


def need(key, why=""):
    value = get(key)
    if value in (None, ""):
        sys.exit(f"{key} is not set in {SITE_DIR}/site.env{' — ' + why if why else ''}")
    return value


def server_conf(name="worldserver.conf"):
    prefix = get("SERVER_PREFIX", "/opt/wow/server")
    return os.path.join(prefix, "etc", name)


def _from_conf(entry):
    """`Host;Port;User;Password;Database` out of the live confs, the way the services always read it.

    Searched in order, because a realm may keep the playerbots DSN in the module's own conf.
    """
    candidates = [server_conf(), server_conf(os.path.join("modules", "playerbots.conf")),
                  server_conf(os.path.join("modules", "mod_ahbot.conf"))]
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith(entry):
                        raw = line.split('"')[1] if '"' in line else line.partition("=")[2].strip()
                        parts = raw.split(";")
                        if len(parts) == 5 and parts[4]:
                            return tuple(parts)
        except (OSError, IndexError):
            continue
    return None


def db(which="characters"):
    """(host, port, user, password, database) for one schema.

    site.env first (DB_HOST, DB_PORT, DB_USER, DB_PASS and DB_AUTH/WORLD/CHARACTERS/PLAYERBOTS for the
    names); the live server config as the fallback, so nothing breaks before site.env exists.
    """
    if which not in DB_KEYS:
        sys.exit(f"unknown database {which!r}; known: {', '.join(sorted(DB_KEYS))}")
    name = get("DB_" + which.upper())
    password = get("DB_PASS")
    if name and password:
        return (get("DB_HOST", "127.0.0.1"), get("DB_PORT", "3306"),
                get("DB_USER", "acore"), password, name)
    found = _from_conf(DB_KEYS[which])
    if found:
        return found
    sys.exit(f"no credentials for the {which} database: set DB_PASS and DB_{which.upper()} in "
             f"{SITE_DIR}/site.env, or keep {DB_KEYS[which]} in {server_conf()}")


def dsn(which="characters"):
    """The same thing as one `Host;Port;User;Password;Database` string."""
    return ";".join(db(which))
