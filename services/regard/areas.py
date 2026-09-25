"""Area names, read out of the client's own AreaTable.dbc (custom wow plans/42).

The names are Blizzard's. They are read off the box at runtime and written only into dashboard-data,
never into this repo -- the same rule the map art follows.

Why a file and not a table. `acore_world.areatable_dbc` exists on this install but holds no rows (the
core reads the .dbc files directly and never populates it), and the worldserver's own AreaTable store
is not exposed over HTTP. The dashboard's /worldmap carries only the 106 WorldMapArea rows, none of
which name the inside of a dungeon -- so Gnomeregan, the Deadmines, the Stockade and nine other places
a journey actually happens in had no name anywhere the dashboard could reach. This file is the only
complete source on the box that does not need the worldserver rebuilt.
"""

import os
import struct

DBC = "/opt/wow/server/data/dbc/AreaTable.dbc"
# A city's districts are not areas at all in 3.3.5a: the Trade District and the Great Forge are named parts of
# the city's buildings, and live here. Field 10 is the area that owns the part, field 11 its name (checked
# 2026-09-24: "Trade District" rows carry 1519, Stormwind City).
WMO_DBC = "/opt/wow/server/data/dbc/WMOAreaTable.dbc"
WMO_OWNER_FIELD = 10

# WDBC is a 20-byte header, then fixed-width records, then one block of NUL-terminated strings that
# every string field indexes into. AreaTable in 3.3.5a is 36 fields of four bytes and the name is
# field 11, the first of sixteen locale slots.
#
# Checked against the running realm 2026-09-22 rather than taken from a layout table: field 11 is the
# one that returned "Gnomeregan", "The Deadmines", "Blackrock Mountain" and "Wailing Caverns" for the
# four area ids the worldserver itself reports for those places in its own snapshot.
NAME_FIELD = 11

# The area this one sits inside (0 for a zone). Checked the same way, 2026-09-24: field 2 of Spirit Rise,
# Hunter Rise and Elder Rise is 1638, Thunder Bluff.
PARENT_FIELD = 2

_cache = {}
_parents = {}


def load(path=DBC):
    """{area id: name} for every area in the file, or {} if it cannot be read.

    Never raises: a missing or unexpected file costs the journeys their place names and nothing else.
    """
    if path not in _cache:
        _cache[path] = _read(path)
    return _cache[path]


def subzones(zone_id, path=DBC):
    """Names of the areas directly inside a zone, e.g. a capital's districts. [] if the file cannot be read."""
    if path not in _parents:
        _parents[path] = _read(path, PARENT_FIELD)
    names = load(path)
    inside = {a for a, parent in _parents[path].items() if parent == zone_id}
    out = {names[a] for a in inside if a in names}
    wmo = os.path.join(os.path.dirname(path), os.path.basename(WMO_DBC))
    if wmo not in _parents:
        _parents[wmo] = _read_pairs(wmo, WMO_OWNER_FIELD, NAME_FIELD)
    out.update(name for owner, name in _parents[wmo] if owner == zone_id or owner in inside)
    out.discard(names.get(zone_id))
    return sorted(out)


def _read_pairs(path, key_field, name_field):
    """[(row[key_field], name)] for every row with a name. [] if the file cannot be read."""
    try:
        data = open(path, "rb").read()
        magic, records, fields, size, _ = struct.unpack("<4s4I", data[:20])
    except (OSError, struct.error):
        return []
    if magic != b"WDBC" or fields * 4 != size or fields <= max(key_field, name_field):
        return []
    strings = data[20 + records * size:]
    out = []
    for i in range(records):
        row = struct.unpack(f"<{fields}I", data[20 + i * size: 20 + (i + 1) * size])
        o = row[name_field]
        if 0 < o < len(strings):
            name = strings[o:strings.find(b"\0", o)].decode("utf-8", "replace")
            if name:
                out.append((row[key_field], name))
    return out


def _read(path, field=NAME_FIELD):
    try:
        data = open(path, "rb").read()
    except OSError:
        return {}
    if len(data) < 20:
        return {}
    magic, records, fields, size, _ = struct.unpack("<4s4I", data[:20])
    if magic != b"WDBC" or fields * 4 != size or fields <= max(NAME_FIELD, field):
        return {}

    body = 20
    strings = data[body + records * size:]

    def text(offset):
        if offset <= 0 or offset >= len(strings):
            return ""
        end = strings.find(b"\0", offset)
        return strings[offset:end].decode("utf-8", "replace") if end >= 0 else ""

    out = {}
    row_fmt = f"<{fields}I"
    for i in range(records):
        row = struct.unpack(row_fmt, data[body + i * size: body + (i + 1) * size])
        if field != NAME_FIELD:
            out[row[0]] = row[field]
            continue
        name = text(row[NAME_FIELD])
        if name:
            out[row[0]] = name
    return out
