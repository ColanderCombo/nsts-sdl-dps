#!/usr/bin/env python3
"""LE `CHANGE old(new)` cards: the renamed csect remembers what it WAS.

The linkage editor's CHANGE statement renames ESD entries in the module the
next module-reading card supplies.  A deck uses it to substitute one variant
of a compool -- differently sized, differently named -- under a csect name
the rest of the configuration is already compiled against.

That leaves the load module and its CONTENT disagreeing about the name, and
both answers are needed:

  * everything address-keyed (placement, relocation, the memory map's csect
    column) is the LOAD MODULE's, i.e. the new name;
  * everything that describes what is IN the csect -- its compilation unit's
    HAL name, its symbol table, the walk of its declarations -- belongs to
    the unit compiled under the OLD name.  Reading the debug data at the new
    name's own stem describes a different unit: a same-named leading field
    set makes the mistake invisible until the point where the variants
    diverge, and then the trailing declaration is silently clipped or lost.

Covers:
  1. applyConcardChanges records the pre-rename name on the renamed SD and
     leaves every other section's origName None; a chain does not overwrite
     the FIRST recorded name.
  2. An ER rename records nothing (only SDs describe content).
  3. The .sym.json section record carries origName only where a rename
     happened.
  4. fcmImage.FcmImage._csect carries it from the manifest to the csect.
  5. mafgen.csects.classify's `stem` override reads the debug map at the
     aliased stem rather than the printed name's.

Run standalone:  build/venv/bin/python test/test_lnk101_change.py
or:              build/venv/bin/python -m pytest test/test_lnk101_change.py -q
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from ap101Utils.fcmImage import FcmImage
from ap101Utils.concard import LayoutOp
from lnk101.linker import AddrDisp, External, Linker, LoadModule, Section
from mafgen.csects import classify

_failures = []
_passes = 0


def check(name, cond, detail=""):
  global _passes
  if cond:
    _passes += 1
  else:
    _failures.append((name, detail))
    print(f"FAIL {name}: {detail}")


def _op(verb, operand):
  return LayoutOp(verb=verb, operand=operand, card='', line_no=0)


def _module(filename, sdNames=(), erNames=()):
  m = LoadModule(filename=filename)
  for i, n in enumerate(sdNames, start=1):
    s = Section(n, i, 'SD', AddrDisp(0), AddrDisp(64), module=m)
    s.baseAddress = _Addr(0x400 + 0x100 * i)
    m.sections[i] = s
    m.sectionsByName[n] = s
  for j, n in enumerate(erNames, start=100):
    e = External(n, j, module=m)
    m.externals[j] = e
    m.externalsByName[n] = e
  return m


class _Addr:
  """Minimal stand-in for lnk101's Addr: halfword accessor only."""

  def __init__(self, hw):
    self.hw = hw


def _linker(program, modules):
  """A Linker with just enough state for applyConcardChanges."""
  lk = object.__new__(Linker)
  lk._concardProgram = program
  lk.modules = modules
  return lk


#
# 1-2. the linker records the pre-rename name
#
def test_change_records_orig_name():
  prog = [_op('CHANGE', 'PAAA(PBBB)'), _op('INCLUDE', 'PAAA')]
  mod = _module('MEMBRA.obj', sdNames=('PAAA', 'PZZZ'))
  _linker(prog, [mod]).applyConcardChanges()
  renamed = mod.sectionsByName.get('PBBB')
  check("rename applied", renamed is not None,
        f"sections: {sorted(mod.sectionsByName)}")
  check("origName recorded", renamed is not None
        and renamed.origName == 'PAAA',
        f"origName={getattr(renamed, 'origName', '<none>')!r}")
  check("old name gone", 'PAAA' not in mod.sectionsByName)
  untouched = mod.sectionsByName['PZZZ']
  check("untouched SD has no origName", untouched.origName is None,
        f"origName={untouched.origName!r}")


def test_change_chain_keeps_first_name():
  """Two decks renaming the same section in turn: the origName is the name
  the CONTENT was compiled under, so the FIRST one wins."""
  mod = _module('MEMBRB.obj', sdNames=('PAAA',))
  _linker([_op('CHANGE', 'PAAA(PBBB)'),
           _op('INCLUDE', 'PAAA')], [mod]).applyConcardChanges()
  _linker([_op('CHANGE', 'PBBB(PCCC)'),
           _op('INCLUDE', 'PBBB')], [mod]).applyConcardChanges()
  s = mod.sectionsByName.get('PCCC')
  check("chained rename applied", s is not None,
        f"sections: {sorted(mod.sectionsByName)}")
  check("chain keeps the original name", s is not None
        and s.origName == 'PAAA',
        f"origName={getattr(s, 'origName', '<none>')!r}")


def test_er_rename_records_nothing():
  """An ER rename redirects a REFERENCE; no content changes hands, so there
  is nothing for a content reader to re-aim."""
  mod = _module('MEMBRC.obj', sdNames=('PKEEP',), erNames=('EROLD',))
  _linker([_op('CHANGE', 'EROLD(ERNEW)'),
           _op('INCLUDE', 'PKEEP')], [mod]).applyConcardChanges()
  check("ER renamed", 'ERNEW' in mod.externalsByName,
        f"externals: {sorted(mod.externalsByName)}")
  check("ER rename leaves SDs alone",
        mod.sectionsByName['PKEEP'].origName is None)


#
# 3. the manifest record
#
def test_manifest_carries_orig_name_only_where_renamed():
  """saveJsonSymbols' section record shape, exercised directly: the key is
  present exactly on renamed sections, so a reader can tell 'not renamed'
  from 'renamed to itself'."""
  mod = _module('MEMBRD.obj', sdNames=('PAAA', 'PZZZ'))
  _linker([_op('CHANGE', 'PAAA(PBBB)'),
           _op('INCLUDE', 'PAAA')], [mod]).applyConcardChanges()
  out = []
  for section in mod.sections.values():
    ent = {"name": section.name, "address": section.baseAddress.hw,
           "size": section.length.hw, "module": mod.name}
    if section.origName is not None:
      ent["origName"] = section.origName
    out.append(ent)
  byName = {e["name"]: e for e in out}
  check("renamed section record has origName",
        byName.get('PBBB', {}).get("origName") == 'PAAA', str(byName))
  check("plain section record omits origName",
        "origName" not in byName.get('PZZZ', {}), str(byName))


#
# 4. it survives into the image's csect list
#
def test_fcmimage_csect_carries_orig_name():
  img = object.__new__(FcmImage)
  ent = {"name": "PBBB", "address": 0x500, "size": 64,
         "module": "MEMBRD", "origName": "PAAA"}
  c = img._csect(ent, "PHASE99")
  check("FcmCsect.origName carried", c.origName == 'PAAA',
        f"origName={c.origName!r}")
  check("FcmCsect.name is the load module's", c.name == 'PBBB')
  plain = img._csect({"name": "PZZZ", "address": 0x600, "size": 8,
                      "module": "MEMBRD"}, "PHASE99")
  check("absent origName reads as None", plain.origName is None,
        f"origName={plain.origName!r}")


#
# 5. the content lookup follows the alias
#
class _Sdf:
  """Two debug-map units whose stems differ only in the aliased letter."""

  def __init__(self):
    self.map = {"CAAAAA": {}, "CBBBBB": {}}

  def get(self, stem):
    return {"CAAAAA": ("UNIT_ALPHA", "COMPOOL"),
            "CBBBBB": ("UNIT_BETA", "COMPOOL")}.get(stem)


def test_classify_stem_override():
  sdf = _Sdf()
  cat, _kind, name = classify("#PCBBBBB"[:8], sdf)
  check("without override, the printed name decides",
        (cat, name) == ("COMPOOL", "UNIT_BETA"), f"{cat} {name}")
  cat, _kind, name = classify("#PCBBBBB"[:8], sdf, stem="CAAAAA")
  check("stem override redirects the content lookup",
        (cat, name) == ("COMPOOL", "UNIT_ALPHA"), f"{cat} {name}")
  # a stem with no debug data must still degrade, not raise
  cat, _kind, name = classify("#PCBBBBB"[:8], sdf, stem="CQQQQQ")
  check("unknown aliased stem degrades to NONHAL",
        (cat, name) == ("NONHAL", None), f"{cat} {name}")


def main():
  for fn in (test_change_records_orig_name,
             test_change_chain_keeps_first_name,
             test_er_rename_records_nothing,
             test_manifest_carries_orig_name_only_where_renamed,
             test_fcmimage_csect_carries_orig_name,
             test_classify_stem_override):
    fn()
  if _failures:
    print(f"\n{len(_failures)} FAILED, {_passes} passed")
    return 1
  print(f"OK: all {_passes} CHANGE-alias checks passed")
  return 0


if __name__ == "__main__":
  sys.exit(main())
