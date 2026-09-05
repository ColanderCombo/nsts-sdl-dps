#!/usr/bin/env python3
"""ap101Utils.members: one member name for every spelling of its file.

A member is stored bare (`FAZ2`) or with an extension (`FAZ2.asm`,
`GKFHOR.hal`, `CG3011.dfg`).  Checks the utility itself and the consumers
that route through it:

  * con80build: SourceIndex resolves an INCLUDE member and a short stem
    under both spellings; PCHnnSRC patch decks are recognised with or
    without `.asm`; the .hal mirror gives a `.hal`-suffixed member one
    extension.
  * dfg: find_deck answers to the display name and to the filename, and
    the encoded display is named for the display.
  * asm101: COPY and on-demand macro members are found with or without
    `.asm`.
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from ap101Utils import members                                  # noqa: E402

_failures = []
_passes = 0


def check(name, cond, detail=""):
  global _passes
  if cond:
    _passes += 1
  else:
    _failures.append((name, detail))
    print(f"FAIL {name}: {detail}")


def test_utility():
  for given, want in (("FAZ2", "FAZ2"), ("FAZ2.asm", "FAZ2"),
                      ("SSSRC/FAZ2.asm", "FAZ2"), ("gen/pp/CD0001.pp.hal", "CD0001"),
                      ("A01XTAB.hal.hal", "A01XTAB"), (Path("x/DCI#STK"), "DCI#STK"),
                      ("PCH10SRC.ASM", "PCH10SRC"), ("dir\\CG3011.dfg", "CG3011")):
    check(f"name({given!r})", members.name(given) == want, members.name(given))
  check("filename adds the suffix once",
        members.filename("GKFHOR.hal", ".hal") == "GKFHOR.hal"
        and members.filename("GKFHOR", ".hal") == "GKFHOR.hal"
        and members.filename(Path("a/FAZ2.asm"), ".obj") == "FAZ2.obj")
  check("spellings: as given, suffixed, bare",
        members.spellings("CG3011") == ["CG3011", "CG3011.asm", "CG3011.hal",
                                        "CG3011.dfg"],
        members.spellings("CG3011"))
  check("spellings of a suffixed name keep it first",
        members.spellings("CG3011.dfg") == ["CG3011.dfg", "CG3011.asm",
                                            "CG3011.hal", "CG3011"],
        members.spellings("CG3011.dfg"))
  with tempfile.TemporaryDirectory() as td:
    a, b = Path(td, "A"), Path(td, "B")
    a.mkdir(); b.mkdir()
    (a / "ONE.hal").write_text("")
    (b / "ONE").write_text("")
    (b / "TWO").write_text("")
    check("find: bare name -> suffixed file", members.find("ONE", [a]) == a / "ONE.hal")
    check("find: suffixed name -> bare file", members.find("TWO.hal", [b]) == b / "TWO")
    check("find: directory order beats spelling",
          members.find("ONE", [b, a]) == b / "ONE")
    check("find: restricted suffixes", members.find("ONE", [a], (".asm",)) is None)
    check("find: absent", members.find("THREE", [a, b]) is None)


def _patch_deck(prefix):
  return f"001      IS    1F4,{prefix},C6C6\n         END\n"


def test_con80build():
  from con80.con80build import (SourceIndex, _hal_mirror, included_csects,
                                 is_patch_source, patch_index, patch_member)

  class Graph:
    def __init__(self, members):
      self._m = members

    def library_members(self):
      return self._m

  check("patch_member bare", patch_member("PCH10SRC") == "PCH10TXT")
  check("patch_member .asm", patch_member("PCH10SRC.asm") == "PCH10TXT")
  check("patch_member Path .ASM", patch_member(Path("SSSRC/PCH10SRC.ASM")) == "PCH10TXT")
  check("is_patch_source", is_patch_source("PCH10SRC.asm") and is_patch_source("PCH11SRC")
        and not is_patch_source("PCH10TXT"))
  with tempfile.TemporaryDirectory() as td:
    sssrc = Path(td, "SSSRC")
    sssrc.mkdir()
    (sssrc / "PCH10SRC.asm").write_text(_patch_deck("#Y101"))
    (sssrc / "PCH11SRC").write_text(_patch_deck("#Y111"))
    (sssrc / "FAZ2.asm").write_text("PHAS2    CSECT\n         END\n")
    (sssrc / "BILDNEW5.asm").write_text("BILDNEW5 CSECT\n         END\n")
    (sssrc / "GKFHOR.hal").write_text(" GKF_HOR: PROCEDURE;\n CLOSE GKF_HOR;\n")
    src = SourceIndex([sssrc])
    check("resolve PCH10TXT -> PCH10SRC.asm",
          src.resolve("PCH10TXT") == sssrc / "PCH10SRC.asm", src.resolve("PCH10TXT"))
    check("resolve PCH11TXT -> PCH11SRC",
          src.resolve("PCH11TXT") == sssrc / "PCH11SRC", src.resolve("PCH11TXT"))
    check("resolve FAZ2 by filename", src.resolve("FAZ2") == sssrc / "FAZ2.asm",
          src.resolve("FAZ2"))
    check("stem6 of a short name has no extension in it",
          src.by_stem6.get("FAZ2") == sssrc / "FAZ2.asm", src.by_stem6.get("FAZ2"))
    check("stem6 of a long name",
          src.by_stem6.get("BILDNE") == sssrc / "BILDNEW5.asm")
    check("resolve GKFHOR -> GKFHOR.hal", src.resolve("GKFHOR") == sssrc / "GKFHOR.hal")
    idx = patch_index(src)
    check("patch_index sees both decks", set(idx) == {"#Y101001", "#Y111001"}, idx)
    provided = included_csects(Graph(["FAZ2", "PCH10TXT"]), src)
    check("included_csects: PHAS2 via the FAZ2 member",
          provided.get("PHAS2") == sssrc / "FAZ2.asm", provided)
    check("included_csects: patch csect via PCH10TXT",
          provided.get("#Y101001") == sssrc / "PCH10SRC.asm", provided)
    haltree = Path(td, "haltree")
    _hal_mirror([sssrc], haltree)
    m = haltree / "SSSRC"
    check("mirror: a .hal member is mirrored with one .hal",
          (m / "GKFHOR.hal").is_symlink() and not (m / "GKFHOR.hal.hal").exists())
    check("mirror: a bare member gains .hal", (m / "PCH11SRC.hal").is_symlink())
    check("mirror: a .asm member is mirrored under its member name",
          (m / "FAZ2.hal").is_symlink())


def test_dfg():
  from dfg.deck import find_deck, resolve_deck
  with tempfile.TemporaryDirectory() as td:
    for d in ("SSSRC", "APPLSRC"):
      Path(td, d).mkdir()
    (Path(td, "APPLSRC", "CG3011.dfg")).write_text(" HEADER=3011G,\n END\n")
    (Path(td, "SSSRC", "MENU12")).write_text(" HEADER=0012,\n END\n")
    dfg = Path(td, "APPLSRC", "CG3011.dfg")
    check("find_deck by display name", find_deck("CG3011", td) == str(dfg))
    check("find_deck by filename", find_deck("CG3011.dfg", td) == str(dfg))
    check("find_deck bare file by suffixed name",
          find_deck("MENU12.dfg", td) == str(Path(td, "SSSRC", "MENU12")))
    check("find_deck absent", find_deck("CG9999", td) is None)
    check("resolve_deck passes a path through", resolve_deck(str(dfg), td) == str(dfg))
  from dfg import encode as _enc
  check("display name drops the deck extension",
        _enc.members.name("APPLSRC/CG3011.dfg") == "CG3011")


def test_asm101():
  from asm101.assemble import Assemble
  with tempfile.TemporaryDirectory() as td:
    lib = Path(td, "MLIB80")
    lib.mkdir()
    (lib / "MSUF.asm").write_text(
        "         MACRO\n         MSUF\n         DC    H'1'\n         MEND\n")
    (lib / "MBARE").write_text(
        "         MACRO\n         MBARE\n         DC    H'2'\n         MEND\n")
    (lib / "CPYSUF.asm").write_text("         DC    H'3'\n")
    (lib / "CPYBARE").write_text("         DC    H'4'\n")
    src = Path(td, "T.asm")
    src.write_text("T        CSECT\n         MSUF\n         MBARE\n"
                   "         COPY  CPYSUF\n         COPY  CPYBARE\n         END\n")
    obj = Path(td, "T.obj")
    a = Assemble(source_files=[src], object_file=obj, libraries=[lib],
                 sysparm="PASS", tolerable_severity=4, verbose=False,
                 debug_info=False, march="ap101s")
    try:
      a.assemble()
      ok = obj.exists()
    except Exception as e:                       # noqa: BLE001
      ok = False
      print("  asm101:", e)
    check("asm101 finds macros and COPY members under both spellings", ok)


def main():
  test_utility()
  test_con80build()
  test_dfg()
  test_asm101()
  print(f"{_passes} passed, {len(_failures)} failed")
  return 1 if _failures else 0


if __name__ == "__main__":
  sys.exit(main())
