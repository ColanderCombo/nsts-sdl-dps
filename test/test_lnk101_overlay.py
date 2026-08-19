#!/usr/bin/env python3
"""Cross-phase resolution picks the definition that SURVIVES the load.

A memory configuration is not a set of phases, it is a SEQUENCE of them: the
composition writes one phase's load module after another and a later phase
simply covers an earlier one.  So a symbol defined in two co-resident phases
can have one definition that is gone by the time the software runs -- the
csect it lived in has been written over -- and a reference resolved to it
points at whatever landed on top.

phaseresolve therefore filters the candidate definitions by whether the csect
holding them is still the csect at that address after the whole composition
has loaded, and takes the first surviving one in phase file order.  Where the
consuming phase belongs to several configurations that disagree about the
answer it declines and keeps the historic lowest-phase pick: one phase load
module serves every composition it appears in, so a rule that cannot give all
of them the same address has nothing to say about that site.

Everything here is synthetic -- made-up phase compositions, csect names and
addresses chosen to exercise the rule, not taken from any build.

Run standalone:  build/venv/bin/python test/test_lnk101_overlay.py
or:              build/venv/bin/python -m pytest test/test_lnk101_overlay.py -q
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from ap101Utils.libModule import Extent, LibModule, LibRld
from ap101Utils.objModule import EsdEntry
from lnk101.phaseresolve import (
    _lookup, _mapConstant, _sectionMap, _survivesLoad, resolvePhases,
)

_failures = []
_passes = 0


def check(name, cond, detail=""):
  global _passes
  if cond:
    _passes += 1
  else:
    _failures.append((name, detail))
    print(f"FAIL {name}: {detail}")


def _mklib(name, cesd, extents=(), rlds=()):
  m = LibModule(name=name)
  m.cesd, m.extents, m.rlds = list(cesd), list(extents), list(rlds)
  return m


# --------------------------------------------------------------------------
# The synthetic build.
#
# Two phases define the same entry point TBLPOINT, each inside its own copy
# of the table csect TBLALPHA:
#
#   PHASE02   TBLALPHA @0x600..0x63F   TBLPOINT @0x610  (halfword 0x308)
#   PHASE12   TBLALPHA @0x200..0x23F   TBLPOINT @0x210  (halfword 0x108)
#
# PHASE12 also places a DIFFERENT csect, TBLOMEGA, at 0x600 -- exactly over
# PHASE02's table.  PHASE09 consumes TBLPOINT through one ACON.
#
# Composition CFGONE loads 2, 9, 12 in that order, so PHASE12's TBLOMEGA is
# the last thing written at 0x600 and PHASE02's TBLPOINT is dead.
# --------------------------------------------------------------------------
P02_ADDR, P12_ADDR = 0x610, 0x210
P02_HW, P12_HW = P02_ADDR // 2, P12_ADDR // 2


def _phase02(overlaid=True):
  """The lower-numbered definer.  `overlaid` only affects PHASE12."""
  return _mklib("PHASE02",
                [EsdEntry.sd(1, "TBLALPHA", 0x600, 0x40),
                 EsdEntry.ld(2, "TBLPOINT", P02_ADDR, 1)],
                [Extent(0x600, bytes(0x40))])


def _phase12(overlaid=True):
  cesd = [EsdEntry.sd(1, "TBLALPHA", 0x200, 0x40),
          EsdEntry.ld(2, "TBLPOINT", P12_ADDR, 1)]
  extents = [Extent(0x200, bytes(0x40))]
  if overlaid:
    cesd.append(EsdEntry.sd(3, "TBLOMEGA", 0x600, 0x40))
    extents.append(Extent(0x600, bytes(0x40)))
  return _mklib("PHASE12", cesd, extents)


def _phase09():
  """The consumer: one fullword ACON at 0x900 citing TBLPOINT."""
  return _mklib("PHASE09",
                [EsdEntry.sd(1, "USERCODE", 0x900, 0x10),
                 EsdEntry.er(2, "TBLPOINT")],
                [Extent(0x900, bytes(0x10))],
                [LibRld(0x1C, 0x900, 2, 1)])


CFGONE = {"CFGONE": [2, 9, 12]}
PHASE_CONFIGS_ONE = {2: frozenset({"CFGONE"}), 9: frozenset({"CFGONE"}),
                     12: frozenset({"CFGONE"})}


def _run(builders, phaseConfigsMap, configPhases):
  """Write the libs, resolve, and return {phase name: patched ACON value}."""
  with tempfile.TemporaryDirectory() as td:
    paths = []
    for lib in builders:
      p = Path(td) / f"{lib.name}.lib"
      lib.write(p)
      paths.append(p)
    res = resolvePhases(sorted(paths), phaseConfigsMap=phaseConfigsMap,
                        configPhases=configPhases)
    out = {}
    for p in paths:
      m = LibModule.read(p)
      for x in m.extents:
        if x.address == 0x900:
          out[m.name] = int.from_bytes(bytes(x.data)[0:4], "big")
    return res, out


def test_overlaid_definition_loses():
  """PHASE02's copy is written over by PHASE12; the reference must take
  PHASE12's."""
  res, val = _run([_phase02(), _phase09(), _phase12()],
                  PHASE_CONFIGS_ONE, CFGONE)
  check("overlay rule: resolved to the surviving definition",
        val.get("PHASE09") == P12_HW,
        f"0x{val.get('PHASE09', 0):04X} want 0x{P12_HW:04X}")
  check("overlay rule: the site was patched",
        res.patched.get("PHASE09") == 1, f"{res.patched}")
  check("overlay rule: no errors", not res.errors, f"{res.errors}")


def test_without_load_order_the_lowest_phase_still_wins():
  """configPhases=None is the historic behaviour, unchanged: whatever the
  composition does with the addresses, the lowest phase file order wins."""
  _res, val = _run([_phase02(), _phase09(), _phase12()],
                   PHASE_CONFIGS_ONE, None)
  check("no load order given -> historic first-in-file-order pick",
        val.get("PHASE09") == P02_HW,
        f"0x{val.get('PHASE09', 0):04X} want 0x{P02_HW:04X}")


def test_surviving_lower_phase_is_still_preferred():
  """The rule is 'still live', not 'last phase wins': drop the overlaying
  csect and PHASE02's definition survives, so it keeps the site."""
  _res, val = _run([_phase02(), _phase09(), _phase12(overlaid=False)],
                   PHASE_CONFIGS_ONE, CFGONE)
  check("nothing overlays -> lowest phase keeps the site",
        val.get("PHASE09") == P02_HW,
        f"0x{val.get('PHASE09', 0):04X} want 0x{P02_HW:04X}")


def test_load_order_not_phase_number_decides():
  """Liveness follows the composition's ORDER, not the phase numbering.  With
  PHASE12 loaded BEFORE PHASE02, PHASE12's TBLOMEGA is written first and
  PHASE02's table lands on top of it -- so PHASE02's definition is the live
  one even though PHASE12 also defines the symbol."""
  _res, val = _run([_phase02(), _phase09(), _phase12()],
                   PHASE_CONFIGS_ONE, {"CFGONE": [12, 9, 2]})
  check("reversed load order -> the later-loaded definition wins",
        val.get("PHASE09") == P02_HW,
        f"0x{val.get('PHASE09', 0):04X} want 0x{P02_HW:04X}")


def test_disagreeing_configurations_decline():
  """A consumer resident in two compositions that overlay DIFFERENTLY gets no
  answer from this rule: one load module cannot hold both addresses, so the
  historic pick stands.

  CFGONE loads 2, 9, 12 (PHASE12's TBLOMEGA buries PHASE02's table, so
  PHASE12's definition is live); CFGTWO loads 2, 9 alone (nothing buries it,
  so PHASE02's is).  PHASE09 is in both."""
  both = {p: frozenset({"CFGONE", "CFGTWO"}) for p in (2, 9)}
  both[12] = frozenset({"CFGONE"})
  _res, val = _run([_phase02(), _phase09(), _phase12()], both,
                   {"CFGONE": [2, 9, 12], "CFGTWO": [2, 9]})
  check("configurations disagree -> historic pick kept",
        val.get("PHASE09") == P02_HW,
        f"0x{val.get('PHASE09', 0):04X} want 0x{P02_HW:04X}")


def test_out_of_scope_definition_is_not_consulted():
  """A definition in a phase the consumer never shares a configuration with
  is out of scope for the pick, live or not: here PHASE12 is in CFGTWO only,
  so PHASE09 (CFGONE) keeps PHASE02's definition."""
  scoped = {2: frozenset({"CFGONE", "CFGTWO"}), 9: frozenset({"CFGONE"}),
            12: frozenset({"CFGTWO"})}
  _res, val = _run([_phase02(), _phase09(), _phase12()], scoped,
                   {"CFGONE": [2, 9], "CFGTWO": [2, 12]})
  check("out-of-scope definition ignored",
        val.get("PHASE09") == P02_HW,
        f"0x{val.get('PHASE09', 0):04X} want 0x{P02_HW:04X}")


def test_phase_in_no_configuration_resolves_globally():
  """A phase in no configuration has no composition to speak for, so there is
  no overlay to test and the global first-in-file-order pick stands."""
  none = {2: frozenset({"CFGONE"}), 12: frozenset({"CFGONE"})}   # not 9
  _res, val = _run([_phase02(), _phase09(), _phase12()], none, CFGONE)
  check("consumer in no configuration -> global pick",
        val.get("PHASE09") == P02_HW,
        f"0x{val.get('PHASE09', 0):04X} want 0x{P02_HW:04X}")


def test_staged_definition_keeps_precedence():
  """A staged definition (displayLoadDefs) is an address the composition
  itself puts the block at, with no defining phase to test for liveness.  It
  is already an observed answer, so the overlay rule stands aside."""
  staged = {"TBLPOINT": (frozenset({"CFGONE"}), 0x800)}
  with tempfile.TemporaryDirectory() as td:
    paths = []
    for lib in (_phase02(), _phase09(), _phase12()):
      p = Path(td) / f"{lib.name}.lib"
      lib.write(p)
      paths.append(p)
    resolvePhases(sorted(paths), phaseConfigsMap=PHASE_CONFIGS_ONE,
                  configPhases=CFGONE, stagedDefs=staged)
    m = LibModule.read(Path(td) / "PHASE09.lib")
    val = int.from_bytes(bytes(m.extents[0].data)[0:4], "big")
  check("staged definition wins over the overlay rule", val == 0x800 // 2,
        f"0x{val:04X} want 0x{0x800 // 2:04X}")


def test_map_constant_needs_disjoint_definer_scopes():
  """Two compositions that share no phase and still place a symbol at ONE
  address are describing the memory MAP, not their own composition, so a
  consumer in a third composition can take it.  Everything weaker declines:
  one definer scope, overlapping scopes, or disagreeing addresses."""
  A, B, C = (frozenset({"CFGA"}), frozenset({"CFGB"}), frozenset({"CFGC"}))
  AB = frozenset({"CFGA", "CFGB"})
  cases = [
    ("disjoint scopes, one address -> resolve",
     [(A, 0x1000, 4), (B, 0x1000, 5)], 0x1000),
    ("three disjoint scopes, one address -> resolve",
     [(A, 0x1000, 4), (B, 0x1000, 5), (C, 0x1000, 6)], 0x1000),
    ("one definer scope -> decline",
     [(A, 0x1000, 4), (A, 0x1000, 5)], None),
    ("overlapping scopes -> decline",
     [(A, 0x1000, 4), (AB, 0x1000, 5)], None),
    ("disjoint scopes, differing addresses -> decline",
     [(A, 0x1000, 4), (B, 0x2000, 5)], None),
    ("single definition -> decline",
     [(A, 0x1000, 4)], None),
    ("a global definer is in scope anyway -> decline",
     [(None, 0x1000, None), (B, 0x1000, 5)], None),
    ("no definition at all -> decline", [], None),
  ]
  for label, cand, want in cases:
    check(f"_mapConstant: {label}", _mapConstant(cand) == want,
          f"got {_mapConstant(cand)!r} want {want!r}")


def test_map_constant_is_the_last_resort():
  """It runs only after both the overlay pick and the in-scope pick have
  failed: an in-scope definition always wins, whatever the others agree on."""
  A, B, C, D = (frozenset({"CFGA"}), frozenset({"CFGB"}),
                frozenset({"CFGC"}), frozenset({"CFGD"}))
  agree = {"SYM": [(A, 0x1000, 4), (B, 0x1000, 5)]}
  check("out-of-scope consumer falls through to the map constant",
        _lookup(agree, "SYM", D) == 0x1000, f"{_lookup(agree, 'SYM', D)!r}")
  check("in-scope consumer takes its own composition's definition",
        _lookup(agree, "SYM", A) == 0x1000, f"{_lookup(agree, 'SYM', A)!r}")
  mixed = {"SYM": [(C, 0x3000, 6), (A, 0x1000, 4), (B, 0x1000, 5)]}
  check("an in-scope definition beats any agreement elsewhere",
        _lookup(mixed, "SYM", C) == 0x3000, f"{_lookup(mixed, 'SYM', C)!r}")
  check("a third, disagreeing definition kills the map constant",
        _lookup(mixed, "SYM", D) is None, f"{_lookup(mixed, 'SYM', D)!r}")
  check("a consumer in no configuration is global, not map-constant",
        _lookup(mixed, "SYM", None) == 0x3000,
        f"{_lookup(mixed, 'SYM', None)!r}")


def test_survives_load_helper():
  """The predicate on its own, so a failure upstream is legible."""
  maps = {2: _sectionMap(_phase02()), 12: _sectionMap(_phase12())}
  check("_survivesLoad: buried definition is not live",
        not _survivesLoad(2, P02_ADDR, [2, 12], maps))
  check("_survivesLoad: unburied definition is live",
        _survivesLoad(12, P12_ADDR, [2, 12], maps))
  check("_survivesLoad: buried definition is live when loaded last",
        _survivesLoad(2, P02_ADDR, [12, 2], maps))
  check("_survivesLoad: the csect that buries it IS live there",
        _survivesLoad(12, P02_ADDR, [2, 12], maps))
  check("_survivesLoad: an address no phase covers is not live",
        not _survivesLoad(12, 0x700, [2, 12], maps))
  check("_sectionMap takes SD entries only, sorted",
        [n for _s, _e, n in maps[12]] == ["TBLALPHA", "TBLOMEGA"],
        f"{maps[12]}")


def main():
  test_overlaid_definition_loses()
  test_without_load_order_the_lowest_phase_still_wins()
  test_surviving_lower_phase_is_still_preferred()
  test_load_order_not_phase_number_decides()
  test_disagreeing_configurations_decline()
  test_out_of_scope_definition_is_not_consulted()
  test_phase_in_no_configuration_resolves_globally()
  test_staged_definition_keeps_precedence()
  test_map_constant_needs_disjoint_definer_scopes()
  test_map_constant_is_the_last_resort()
  test_survives_load_helper()
  if _failures:
    print(f"FAILED {len(_failures)} overlay check(s):")
    for name, detail in _failures:
      print(f"  - {name}: {detail}")
    return 1
  print(f"OK: all {_passes} overlay checks passed")
  return 0


if __name__ == "__main__":
  sys.exit(main())
