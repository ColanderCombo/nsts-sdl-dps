#!/usr/bin/env python3
#
# The Keyboard/Value Table (KVT) — the display's ITEM entries and limit tables.
#
# Each `ITEM=(n,class,fmt[,min,max])` deck directive becomes a two-word KVT entry
# describing a keyboard-entry or display field: its class, format, sign/scale, and
# (when bounded) an index into a limit table.  Integer items index a 32-bit limit
# table; scaled (`Sm.n`) items index a separate IBM-hex-float limit table.  The
# 3-word KVT header carries the item count and the offsets to those two tables.
#
import re
from decimal import Decimal, InvalidOperation

from ap101Utils.ibmhex import ibm_double
from .model import Error

# format type letter -> KVT type nibble (entry word0 bits 12-15)
_TYPE_CODE = {"I": 1, "H": 2}


def _mkrange(a, b):
    """Range tuple for a limit table; ints for plain items, Decimals for scaled
    (float) limits.  Decimal normalises the limit so value-equal ranges written
    differently ('.000' vs '0.000') dedup to one range index."""
    try:
        return (int(a), int(b))
    except ValueError:
        try:
            return (Decimal(a.strip()), Decimal(b.strip()))
        except InvalidOperation:                 # e.g. hex '0A,0B' -> keep raw
            return (a.strip(), b.strip())


def build_kvt(ds):
    """Return (header3, body, srtab, irtab, item_echoes, nitems).

    header3   — the 3-word KVT header (word0 = item count + KEYS flag; words 1,2
                are the limit-table offsets, patched in by the caller).
    body      — two words per item.
    srtab     — scaled (IBM-double) limit table; irtab — integer limit table.
    item_echoes — one `-- ITEM = (...)` echo string per body pair, for comments.
    """
    items = [v for k, v in ds if k == "ITEM" and re.match(r"\(\d+", v)]
    if not items:
        return [0, 0, 0], [], [], [], [], 0
    # Integer (I/H) and scaled (S) items index SEPARATE range tables, each with
    # its own 1-based range index.  Scaled limits are IBM-double floats (header
    # word1 points there); integer limits are 32-bit (header word2 points there).
    iranges, iidx, sranges, sidx = [], {}, [], {}
    body, echoes = [], []

    def rngidx(rng, ranges, idx):
        """1-based index of `rng` in its limit table (0 = unbounded),
        appending a first-seen range."""
        if rng and rng not in idx:
            idx[rng] = len(ranges) + 1
            ranges.append(rng)
        return idx.get(rng, 0)

    for it in items:
        echoes.append("-- ITEM = " + it.strip())
        p = [x.strip() for x in it.strip("()").split(",")]
        cls = p[1] if len(p) > 1 else ""
        if len(p) < 2:                           # bare ITEM=(nn) -> placeholder
            body += [0x0000, 0x0000]; continue
        if cls == "X":                           # execute -> 4000
            body += [0x4000, 0]; continue
        if cls == "UX":                          # underscored execute -> 4008
            body += [0x4008, 0]; continue
        fmt = p[2]
        sub = 4 if cls == "UE" else 0            # UE=keyboard entry, D=display item
        if len(p) >= 5 and fmt[:1] == "H":
            # H-format (hex-conversion) items take HEX limits:
            # ITEM=(n,UE,H1,0A,0B) stores 0x0A/0x0B in the integer limit
            # table.
            try:
                rng = (int(p[3], 16), int(p[4], 16))
            except ValueError:
                rng = _mkrange(p[3], p[4])
        else:
            rng = _mkrange(p[3], p[4]) if len(p) >= 5 else None
        if fmt[:1] == "S":                       # scaled Sm.n -> scale = m*100+n
            mn = fmt[1:].split(".")
            scale = int(mn[0]) * 100 + (int(mn[1]) if len(mn) > 1 and mn[1] else 0)
            body += [0x0800 | (rngidx(rng, sranges, sidx) << 4) | sub, scale]
            continue
        if fmt[:1] not in _TYPE_CODE:            # unknown item format: refuse
            raise Error("unknown ITEM format %r" % (fmt[:1],))
        body += [(_TYPE_CODE[fmt[:1]] << 12) | (rngidx(rng, iranges, iidx) << 4)
                 | sub, int(fmt[1:]) if fmt[1:].isdigit() else 0]
    srtab = []                                   # scaled: IBM-double min then max
    for mn, mx in sranges:
        srtab += ibm_double(mn) + ibm_double(mx)
    irtab = []                                   # integer: 32-bit min then max
    for mn, mx in iranges:
        if isinstance(mn, int) and isinstance(mx, int):
            irtab += [(mn >> 16) & 0xFFFF, mn & 0xFFFF, (mx >> 16) & 0xFFFF, mx & 0xFFFF]
        else:                                    # non-integer limits: refuse
            raise Error("non-integer ITEM limits %r..%r" % (mn, mx))
    # word0 low byte = keyboard-entry-class flag from the KEYS directive:
    # KEYS=(X) -> 0x40 (the X item-class code), else 0.
    keys = next((v for k, v in ds if k == "KEYS"), None)
    kflag = {"X": 0x40}.get(keys.strip("()").strip(), 0) if keys else 0
    return [(len(items) << 8) | kflag, 0, 0], body, srtab, irtab, echoes, \
        len(items)
