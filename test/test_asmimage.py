#!/usr/bin/env python3
"""test_asmimage -- assembling and locating a standalone AP-101 program.

Checked here: that locating resolves the addressing forms an IOP program
uses -- an 18-bit absolute address, a negative addend on a command table
base, a program-counter-relative displacement -- and that ext/sim's
committed fakeipl image still matches the source beside it.

Run:  python3 test/test_asmimage.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tools import asmimage                      # noqa: E402

passed = failed = 0


def check(label, got, want):
    global passed, failed
    if got == want:
        passed += 1
    else:
        failed += 1
        print(f"FAIL  {label}: got {got!r}, want {want!r}")


def ok(label, cond):
    check(label, bool(cond), True)


def words(image):
    return [int(w, 16) for w in image["image"]]


SOURCE = """\
ASMIMG   CSECT
ASMMSC   DS    0F
         @LF   ASMDATA
         @SIO  0
         @WAT  0
ASMBCE   DS    0F
         #LBR  0
         #CMD  ASMTBL
         #SST  ASMDATA
         #WAT  0
         DS    0F
ASMTBL   EQU   *-36
ASMCMDW  DC    A(0)
         DC    A(0)
ASMDATA  DC    F'-1'
         END
"""

ORIGIN = 0x3FF00


def located():
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "asmimg.asm"
        src.write_text(SOURCE)
        return asmimage.build(src, ORIGIN)


img = located()
w = words(img)
S = img["symbols"]

check("the image is located where it was asked for", img["origin"], ORIGIN)
check("the image is as long as the csect", img["length"], len(w))
check("the name is the csect's", img["name"], "ASMIMG")

# An 18-bit absolute address in a long instruction, relocated to the origin.
check("@LF addresses the located data cell",
      (w[0] << 16) | w[1], 0xf4000000 | S["ASMDATA"])
check("#LBR keeps an absolute zero",
      (w[S["ASMBCE"] - ORIGIN] << 16) | w[S["ASMBCE"] - ORIGIN + 1], 0xf2000000)

# A command table base is written as the first entry less 36, so a negative
# addend has to survive relocation.
check("#CMD addresses the table base",
      (w[S["ASMBCE"] - ORIGIN + 2] << 16) | w[S["ASMBCE"] - ORIGIN + 3],
      0xfe000000 | (S["ASMCMDW"] - 36))
check("the table base is a symbol of its own", S["ASMTBL"], S["ASMCMDW"] - 36)

# A short memory-reference instruction carries target - (address + 1).
sst = S["ASMBCE"] + 4
check("#SST is displaced to the data cell",
      w[sst - ORIGIN], 0x5000 | ((S["ASMDATA"] - (sst + 1)) & 0x7ff))

check("DC F'-1' assembles to -1", (w[S["ASMDATA"] - ORIGIN] << 16) |
      w[S["ASMDATA"] - ORIGIN + 1], 0xffffffff)
check("an untouched command table entry is zero", w[S["ASMCMDW"] - ORIGIN], 0)

ok("the source hash is recorded", len(img["sourceSha256"]) == 64)

# The committed image the simulator embeds, against the source beside it.
asm = ROOT / "ext/sim/gpc/asm/fakeipl.asm"
gen = ROOT / "ext/sim/gpc/gen/fakeipl.json"
if asm.exists() and gen.exists():
    have = json.loads(gen.read_text())
    fresh = asmimage.build(asm, have["origin"])
    for key in ("name", "origin", "length", "image", "symbols", "sourceSha256"):
        check(f"{gen.name} is current: {key}", have[key], fresh[key])
else:
    print(f"skipped: {gen} is not in the tree")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
