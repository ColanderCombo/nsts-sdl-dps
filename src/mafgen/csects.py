"""Csect records and name-driven categorization (MAFGEN's csect classes)."""
import re
from pathlib import Path

from .sdfnames import SdfNameMap

# csect-name prefix -> (category key, needs SDF stem lookup)
# A csect whose stem has no SDF is assumed NONHAL, as MAFGEN reports it.
_PATCH_RE = re.compile(r"^[$#][XY][0-9]|^(PCH|PHP)")

class Csect:
    __slots__ = ("name", "start", "size", "cat", "kind", "halname",
                 "module", "phase", "blockname", "reserved")

    def __init__(self, name, start, size, cat, kind=None, halname=None,
                 module=None, phase=None, reserved=False):
        self.name, self.start, self.size = name, start, size
        self.cat, self.kind, self.halname = cat, kind, halname
        self.module, self.phase = module, phase
        self.blockname = None       # internal blocks: the block's own name
        self.reserved = reserved    # RESERVE-carded: bracketed in the map

    @property
    def end(self):          # inclusive, DASS style
        return self.start + self.size - 1


# the AS TYPE vocabulary the deck can assign (other clauses -- STATUS
# INVARIANT alone -- leave the name-driven classification in force)
_DECK_TYPES = {"MSC", "BCE", "PATCH"}
_DECK_STMT = re.compile(r"DEFINE\s+CSECTS\s+(.*?)\s+AS\s+(.*)", re.S)


def deck_types(path):
    """{csect name: category} from a MAFGEN control deck (TOOLIN/MAFTWO
    card images): 'DEFINE CSECTS a, b, ... AS TYPE MSC;' types every
    csect in the statement.  IOP (MSC/BCE) programs and non-PCH-named
    patch areas are indistinguishable from NONHAL by name -- flight
    MAFGEN was TOLD, via this deck, exactly as we are here."""
    text = " ".join(ln[:72] for ln in
                    Path(path).read_text().splitlines())
    out = {}
    for stmt in text.split(";"):
        m = _DECK_STMT.search(stmt)
        if not m:
            continue
        t = re.match(r"TYPE\s+([A-Z]+)", m.group(2))
        if not t or t.group(1) not in _DECK_TYPES:
            continue
        for name in re.findall(r"[^\s,]+", m.group(1)):
            out[name] = t.group(1)
    return out


def classify(name: str, sdf: SdfNameMap, deck=None):
    """(category key, block kind or None, HAL name or None).

    Categories: PROGRAM COMSUB INTERNAL DATA COMPOOL ZCON QCON PDE
    EXCLUSIVE STACK LIBDATA LIBCODE PATCH NONHAL MSC BCE.
    Anything needing an SDF that has none degrades to NONHAL (MAFGEN's
    "WILL BE ASSUMED NONHAL").
    """
    if deck and name in deck:
        return deck[name], None, None
    if _PATCH_RE.match(name):
        return "PATCH", None, None
    p2, stem = name[:2], name[2:]
    if p2 == "#Q":
        return "QCON", None, None
    if p2 == "#L":
        return "LIBDATA", None, None
    if p2 == "#0":
        return "LIBCODE", None, None
    sdfed = {"#Z": "ZCON", "#E": "PDE", "#D": "DATA", "#P": "COMPOOL",
             "#X": "EXCLUSIVE", "#C": "COMSUB", "$0": "PROGRAM"}.get(p2)
    if p2.startswith("@"):
        sdfed = "STACK"
    elif len(name) == 8 and name[0] in "AB" and name[1].isdigit():
        # internal-block csects: letter encodes nesting (A1.., B0DMPMMM)
        sdfed = "INTERNAL"
    if sdfed:
        hit = sdf.get(stem)
        if hit:
            halname, kind = hit
            if sdfed == "PROGRAM":
                kind = "PROGRAM"
            return sdfed, kind, halname
    return "NONHAL", None, None
