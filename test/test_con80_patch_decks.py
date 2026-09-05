#!/usr/bin/env python3
"""con80build patch-deck (PCHnnSRC/PCHnnTXT) detection.

A patch deck's source filename may or may not carry a trailing .asm
extension -- OI301700 has always shipped PCHnnSRC.asm, and OI340600's
own PCHnnSRC files gained the extension in 00d1051b (they are ordinary
AP-101S assembly and ASM101S/ASM101Sa refuse to assemble a source file
whose name doesn't end in .asm).  patch_member()/SourceIndex.resolve()/
patch_index()/included_csects()/resolve_worklist() all key off the
PCHnnSRC/PCHnnTXT filename shape and must recognize it regardless of
which spelling is on disk -- confirmed here for both.
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from con80.con80build import SourceIndex, patch_csects, patch_index, patch_member

_failures = []
_passes = 0


def check(name, cond, detail=""):
  global _passes
  if cond:
    _passes += 1
  else:
    _failures.append((name, detail))
    print(f"FAIL {name}: {detail}")


def test_patch_member_naming():
  check("bare stem", patch_member("PCH10SRC") == "PCH10TXT",
        patch_member("PCH10SRC"))
  check(".asm-suffixed", patch_member("PCH10SRC.asm") == "PCH10TXT",
        patch_member("PCH10SRC.asm"))
  check(".asm suffix is case-insensitive",
        patch_member("PCH10SRC.ASM") == "PCH10TXT",
        patch_member("PCH10SRC.ASM"))


def _patch_deck(prefix: str) -> str:
  return f"001      IS    1F4,{prefix},C6C6\n         END\n"


def _write_deck(sssrc: Path, name: str, prefix: str):
  (sssrc / name).write_text(_patch_deck(prefix))


def test_source_index_and_patch_index():
  with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    sssrc = root / "SSSRC"
    sssrc.mkdir()
    _write_deck(sssrc, "PCH10SRC.asm", "#Y101")   # current on-disk spelling
    _write_deck(sssrc, "PCH11SRC", "#Y111")       # legacy extensionless spelling

    src = SourceIndex([sssrc])

    check("resolve PCH10TXT -> PCH10SRC.asm",
          src.resolve("PCH10TXT") == sssrc / "PCH10SRC.asm",
          src.resolve("PCH10TXT"))
    check("resolve PCH11TXT -> PCH11SRC",
          src.resolve("PCH11TXT") == sssrc / "PCH11SRC",
          src.resolve("PCH11TXT"))

    idx = patch_index(src)
    check("patch_index sees both decks", len(idx) == 2, idx)
    check("patch_index: #Y101001 -> PCH10SRC.asm",
          idx.get("#Y101001") == sssrc / "PCH10SRC.asm", idx)
    check("patch_index: #Y111001 -> PCH11SRC",
          idx.get("#Y111001") == sssrc / "PCH11SRC", idx)


def test_patch_csects_unaffected_by_extension():
  with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    sssrc = root / "SSSRC"
    sssrc.mkdir()
    _write_deck(sssrc, "PCH12SRC.asm", "#Y121")
    check("patch_csects reads the IS line regardless of filename",
          patch_csects(sssrc / "PCH12SRC.asm") == ["#Y121001"],
          patch_csects(sssrc / "PCH12SRC.asm"))


def main():
  test_patch_member_naming()
  test_source_index_and_patch_index()
  test_patch_csects_unaffected_by_extension()
  print(f"{_passes} passed, {len(_failures)} failed")
  return 1 if _failures else 0


if __name__ == "__main__":
  sys.exit(main())
