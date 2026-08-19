#!/usr/bin/env python3
"""Blank-ddname LIBRARY cards: RESTRICTED NO-CALL and NEVER-CALL.

The linkage editor's LIBRARY statement has three forms, and the two with a
BLANK ddname suppress automatic library call rather than adding a library:

  * `LIBRARY *(er,...)`      restricted no-call -- the named EXTERNAL
                             REFERENCES are never resolved, silently, and
                             the load module keeps the assembled text with
                             no RLD fixup applied.
  * `LIBRARY  (member,...)`  never-call -- the named library MEMBERS are
                             never fetched.
  * `LIBRARY ddname(member)` an additional call library (neither).

Covers:
  1. concard.library_no_call separates the three forms and keeps a trailing
     change-request annotation, which a deck may write straight after the
     closing paren, out of the name lists.
  2. phaseresolve.restrictedNoCall scoping: a card in phase Q binds a site
     in phase P exactly when configs(P) <= configs(Q).  Two phases confined
     to the same single configuration therefore inherit each other's cards,
     a base phase's card reaches the overlay phases above it but not the
     other way round, and a phase present in EVERY configuration is out of
     reach of every single-configuration card.
  3. resolvePhases honours it: the restricted site keeps its text, is
     counted in result.restricted rather than result.remaining, and gets no
     manifest record, while the never-call MEMBER name restricts nothing.

Run standalone:  build/venv/bin/python test/test_lnk101_nocall.py
or:              build/venv/bin/python -m pytest test/test_lnk101_nocall.py -q
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from ap101Utils.concard import ConcardDeck, library_no_call
from ap101Utils.libModule import Extent, LibModule, LibRld
from ap101Utils.objModule import EsdEntry
from lnk101.phaseresolve import resolvePhases, restrictedNoCall

_failures = []
_passes = 0


def check(name, cond, detail=""):
  global _passes
  if cond:
    _passes += 1
  else:
    _failures.append((name, detail))
    print(f"FAIL {name}: {detail}")


# 72-column card body + SRN field, CRLF line ends like the source tree.
def card(text, srn="000100AA"):
  return f"{text:<72s}{srn}\r\n"


# One card of each form, plus the annotated variant: a deck may write a
# change-request id straight after the closing paren, so the parse must stop
# at the first ")".
STUB = (card("         LIBRARY *(SYMONE,SYMTWO)")
        + card("         LIBRARY *(SYMTHR,SYMFOR) DR12345")
        + card("         LIBRARY  (MEMONE,MEMTWO)")
        + card("         LIBRARY OTHRLIB(OTHRMEM)")
        + card("         INSERT  MEMONE"))


def test_library_no_call_forms():
  with tempfile.TemporaryDirectory() as td:
    (Path(td) / "STUB").write_text(STUB, newline="")
    deck = ConcardDeck(td)
    restricted, never = library_no_call(deck, "STUB")
  check("star form -> restricted symbols",
        restricted == {"SYMONE", "SYMTWO", "SYMTHR", "SYMFOR"},
        f"{sorted(restricted)}")
  check("blank-ddname form -> never-call members",
        never == {"MEMONE", "MEMTWO"}, f"{sorted(never)}")
  check("named ddname is not a no-call",
        "OTHRMEM" not in restricted and "OTHRMEM" not in never)
  check("trailing change-request annotation does not leak into a symbol",
        not any("DR12345" in s for s in restricted | never))


# A configuration/phase membership with the shapes the scoping rule turns
# on, inverted the way phaseConfigs yields it.  Phases 1 and 2 are in every
# configuration; 4 is shared by CFGC and CFGD with one overlay phase each (5
# and 6); 7 and 8 are both confined to CFGE alone.
CONFIGS = {"CFGA": (1, 2),
           "CFGB": (1, 2, 3),
           "CFGC": (1, 2, 4, 5),
           "CFGD": (1, 2, 4, 6),
           "CFGE": (1, 2, 7, 8)}


def _phaseConfigsMap():
  m = {}
  for name, phases in CONFIGS.items():
    for p in phases:
      m.setdefault(p, set()).add(name)
  return {p: frozenset(s) for p, s in m.items()}


# Phase decks carrying just enough for restrictedNoCall: phase 2 is in every
# configuration, phase 4 is shared by CFGC and CFGD, phase 5 is CFGC-only,
# and phases 7 and 8 are both CFGE-only.
SCOPE_DECK = {
    "PHASE02": "         LIBRARY *(SYMALL)",
    "PHASE04": "         LIBRARY *(SYMBOTH)",
    "PHASE05": "         LIBRARY *(#SYMC)",
    "PHASE07": "         LIBRARY *(SYME1,SYME2,SYME3)",
    "PHASE08": "         LIBRARY *(SYME4)",
}


def test_restricted_scope_follows_config_containment():
  with tempfile.TemporaryDirectory() as td:
    for name, body in SCOPE_DECK.items():
      (Path(td) / name).write_text(card(body), newline="")
    r = restrictedNoCall(ConcardDeck(td), _phaseConfigsMap())
  # CFGE = 7+8: each is confined to CFGE alone, so the containment holds
  # both ways and the two phases inherit each other's cards.
  check("phase 8 inherits the phase-7 (CFGE) cards",
        {"SYME1", "SYME2", "SYME3", "SYME4"} <= r[8], f"{sorted(r[8])}")
  check("phase 7 inherits the phase-8 (CFGE) card", "SYME4" in r[7],
        f"{sorted(r[7])}")
  # CFGC = 4+5: the shared phase's card reaches the overlay, not the other
  # way -- phase 4 is also in CFGD, so a CFGC-only card must not bind it.
  check("phase 5 inherits the phase-4 (CFGC+CFGD) card", "SYMBOTH" in r[5],
        f"{sorted(r[5])}")
  check("phase 4 does not take the CFGC-only phase-5 card",
        "#SYMC" not in r[4], f"{sorted(r[4])}")
  check("phase 6 (CFGD) inherits phase 4 but not phase 5",
        r[6] == frozenset({"SYMBOTH", "SYMALL"}), f"{sorted(r[6])}")
  # Phase 2 is in every configuration, so no single-config card reaches it.
  check("phase 2 takes its own card only", r[2] == frozenset({"SYMALL"}),
        f"{sorted(r[2])}")
  check("every phase inherits the all-config phase-2 card",
        all("SYMALL" in v for v in r.values()))


def _mklib(name, cesd, extents, rlds):
  m = LibModule(name=name)
  m.cesd, m.extents, m.rlds = cesd, extents, rlds
  return m


def test_resolve_honours_restricted_nocall():
  """An ACON site whose ER a sibling phase DOES define.  Without the card
  it resolves; with it the four text bytes stay exactly as assembled, the
  site is reported as restricted rather than remaining, and the never-call
  MEMBER name restricts nothing."""
  def libs():
    a = _mklib("PHASE18", [EsdEntry.sd(1, "CONSUMER", 0x100, 0x08),
                           EsdEntry.er(2, "EXTONE"),
                           EsdEntry.er(3, "EXTTWO")],
               [Extent(0x100, bytes(8))],
               [LibRld(0x1C, 0x100, 2, 1),      # ACON -> EXTONE
                LibRld(0x00, 0x106, 3, 1)])     # YCON -> EXTTWO
    b = _mklib("PHASE02", [EsdEntry.sd(1, "PROVIDER", 0x400, 0x10),
                           EsdEntry.ld(2, "EXTONE", 0x408, 1),
                           EsdEntry.ld(3, "EXTTWO", 0x40C, 1)],
               [Extent(0x400, bytes(16))], [])
    return a, b

  with tempfile.TemporaryDirectory() as td:
    pa, pb = Path(td) / "PHASE18.lib", Path(td) / "PHASE02.lib"
    a, b = libs()
    a.write(pa)
    b.write(pb)
    base = resolvePhases([pa, pb], dryRun=True)
    check("baseline resolves both sites",
          base.patched.get("PHASE18") == 2, f"{base.patched}")

    a, b = libs()
    a.write(pa)
    b.write(pb)
    res = resolvePhases([pa, pb], restrictedMap={18: frozenset({"EXTONE"})})
    check("restricted site not patched",
          res.patched.get("PHASE18") == 1, f"{res.patched}")
    check("restricted site counted as no-called",
          res.restricted.get("PHASE18") == {"EXTONE": 1}
          and res.totalRestricted == 1, f"{res.restricted}")
    check("restricted site is not 'remaining'",
          "EXTONE" not in res.remaining.get("PHASE18", {}),
          f"{res.remaining}")
    data = LibModule.read(pa).extents[0].data
    # bytes 0-3 = the restricted ACON, untouched; bytes 6-7 = the YCON that
    # still resolves (EXTTWO's LD at byte 0x40C = halfword 0x206).
    check("restricted text left as assembled", data[0:4] == bytes(4),
          data.hex())
    check("unrestricted site still resolved",
          data[6:8] == bytes.fromhex("0206"), data.hex())
    check("no errors", not res.errors, f"{res.errors}")

    # The MEMBER-name (never-call) form is a different list: passing a
    # member name where a symbol is expected must restrict nothing.
    a, b = libs()
    a.write(pa)
    b.write(pb)
    nv = resolvePhases([pa, pb], dryRun=True,
                       restrictedMap={18: frozenset({"PROVIDER"})})
    check("never-call member name restricts no symbol",
          nv.patched.get("PHASE18") == 2 and not nv.totalRestricted,
          f"{nv.patched} {nv.restricted}")


def main():
  test_library_no_call_forms()
  test_restricted_scope_follows_config_containment()
  test_resolve_honours_restricted_nocall()
  if _failures:
    print(f"FAILED {len(_failures)} no-call check(s):")
    for name, detail in _failures:
      print(f"  - {name}: {detail}")
    return 1
  print(f"OK: all {_passes} no-call checks passed")
  return 0


if __name__ == "__main__":
  sys.exit(main())
