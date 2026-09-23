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

import struct

DBC = "/opt/wow/server/data/dbc/AreaTable.dbc"

# WDBC is a 20-byte header, then fixed-width records, then one block of NUL-terminated strings that
# every string field indexes into. AreaTable in 3.3.5a is 36 fields of four bytes and the name is
# field 11, the first of sixteen locale slots.
#
# Checked against the running realm 2026-09-22 rather than taken from a layout table: field 11 is the
# one that returned "Gnomeregan", "The Deadmines", "Blackrock Mountain" and "Wailing Caverns" for the
# four area ids the worldserver itself reports for those places in its own snapshot.
NAME_FIELD = 11

_cache = {}


def load(path=DBC):
    """{area id: name} for every area in the file, or {} if it cannot be read.

    Never raises: a missing or unexpected file costs the journeys their place names and nothing else.
    """
    if path not in _cache:
        _cache[path] = _read(path)
    return _cache[path]


def _read(path):
    try:
        data = open(path, "rb").read()
    except OSError:
        return {}
    if len(data) < 20:
        return {}
    magic, records, fields, size, _ = struct.unpack("<4s4I", data[:20])
    if magic != b"WDBC" or fields * 4 != size or fields <= NAME_FIELD:
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
        name = text(row[NAME_FIELD])
        if name:
            out[row[0]] = name
    return out
