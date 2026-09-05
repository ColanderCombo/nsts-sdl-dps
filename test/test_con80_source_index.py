#!/usr/bin/env python3
"""con80build SourceIndex.resolve() for short (<=6-char) filenames.

An object-module/csect name that con80build looks up (an INCLUDE
member, a bare csect reference) never carries a .asm extension, but
every source file on disk does.  For a filename longer than 6
characters this is a non-issue: by_stem6's 6-char window never reaches
the extension.  But for a filename whose extensionless stem is 6
characters or shorter -- e.g. SSSRC/FAZ2.asm, whose own CSECT is
PHAS2, so it is found only via its filename, through the INCLUDE
SYSLIBL1(...,FAZ2,...) member name -- the window used to bleed into
the extension: FAZ2.asm[:6] == "FAZ2.a", not "FAZ2", so resolve("FAZ2")
returned None and con80build reported FAZ2 unresolved (PHASE10) even
though its CSECT (PHAS2) linked in fine via the independent
csect_defs() scan.  by_name/by_stem6 now index every file under its
extensionless stem, not just its on-disk name.
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from con80.con80build import SourceIndex, included_csects

_failures = []
_passes = 0


def check(name, cond, detail=""):
  global _passes
  if cond:
    _passes += 1
  else:
    _failures.append((name, detail))
    print(f"FAIL {name}: {detail}")


class _FakeGraph:
  """Just enough of concard.Graph for included_csects()."""
  def __init__(self, members):
    self._members = members

  def library_members(self):
    return self._members


def test_short_stem_resolves_despite_asm_extension():
  with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    sssrc = root / "SSSRC"
    sssrc.mkdir()
    # Mirrors SSSRC/FAZ2.asm: a 4-char stem whose own CSECT (PHAS2) is a
    # different name than the file -- the file is only ever looked up by
    # its own filename, via an INCLUDE member reference.
    (sssrc / "FAZ2.asm").write_text("PHAS2    CSECT\n         END\n")
    # A long-named file's [:6] window is unaffected either way -- sanity
    # check that the general fix didn't disturb that existing behavior.
    (sssrc / "BILDNEW5.asm").write_text("BILDNEW5 CSECT\n         END\n")

    src = SourceIndex([sssrc])

    check("resolve('FAZ2') finds FAZ2.asm by its own filename",
          src.resolve("FAZ2") == sssrc / "FAZ2.asm", src.resolve("FAZ2"))
    check("stem6 lookup for a short name also resolves",
          src.by_stem6.get("FAZ2") == sssrc / "FAZ2.asm",
          src.by_stem6.get("FAZ2"))
    check("a long name's 6-char stem is untouched by the extension",
          src.by_stem6.get("BILDNE") == sssrc / "BILDNEW5.asm",
          src.by_stem6.get("BILDNE"))

    # The real bug: included_csects() uses resolve("FAZ2") to open the
    # file and harvest the csects it defines (PHAS2), for an INSERT PHAS2
    # elsewhere in the deck to find.
    graph = _FakeGraph(["FAZ2"])
    provided = included_csects(graph, src)
    check("included_csects() maps PHAS2 -> FAZ2.asm via the FAZ2 member name",
          provided.get("PHAS2") == sssrc / "FAZ2.asm", provided)


def main():
  test_short_stem_resolves_despite_asm_extension()
  print(f"{_passes} passed, {len(_failures)} failed")
  return 1 if _failures else 0


if __name__ == "__main__":
  sys.exit(main())
