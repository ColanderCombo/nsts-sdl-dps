#!/usr/bin/env python3
#
# ap101Utils.libModule (.lib load-module container) regression tests:
#   1. UNIT: full write/read roundtrip of every record type.
#   2. INTEGRATION: assemble two tiny decks (one with SPON/SPOFF), link them
#      with lnk101 --lib, and gate the .lib CESD, text extents, and the
#      layered protection (PROT-card ranges override the class default).
#
# Run standalone:  build/venv/bin/python test/test_libmodule.py
# or via ctest:    ctest -R libmodule
#
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from ap101Utils.libModule import Extent, LibModule, LibRld, Region
from ap101Utils.objModule import EsdEntry, EsdType

_failures = []
_passes = 0


def check(name, cond, detail=""):
  global _passes
  if cond:
    _passes += 1
  else:
    _failures.append(f"{name}: {detail}" if detail else name)


def unit_roundtrip():
  m = LibModule(name="ROUNDTRP", linkerId="LNK101 test",
                created="2026-07-18T00:00:00")
  m.cesd = [EsdEntry.sd(1, "CODEA", 0x100, 0x40),
            EsdEntry.ld(2, "CODEAE1", 0x110, 1),
            EsdEntry.er(3, "MISSING")]
  data = bytes(range(64))
  prot = [(i % 3 == 0) for i in range(32)]
  m.extents = [Extent(0x100, data, prot)]
  m.rlds = [LibRld(0x10, 0x104, 1, 1), LibRld(0x50, 0x120, 3, 1)]
  m.regions = [Region("OVLY1", 0x100, 0x140, 0)]
  m.phaseRoot = "PHASE02"
  m.phaseNumbers = [2, 3]
  m.nocall = True
  m.entryAddress = 0x110
  m.entryName = "CODEAE1"
  m.linkState = {"zconPoolNext": 0x494}

  with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "rt.lib"
    m.write(p)
    r = LibModule.read(p)

  check("rt_idr", r.name == "ROUNDTRP" and r.linkerId == "LNK101 test")
  check("rt_cesd_types",
        [e.type for e in r.cesd] == [EsdType.SD, EsdType.LD, EsdType.ER],
        f"{[e.typeName for e in r.cesd]}")
  check("rt_cesd_sd", r.cesd[0].address == 0x100 and r.cesd[0].length == 0x40)
  check("rt_cesd_ld", r.cesd[1].address == 0x110 and r.cesd[1].ldid == 1)
  check("rt_text", r.extents[0].address == 0x100
        and r.extents[0].data == data)
  check("rt_prot", r.extents[0].protect == prot,
        f"{r.extents[0].protect!r}")
  check("rt_rld", len(r.rlds) == 2 and r.rlds[1].relId == 3
        and r.rlds[1].position == 0x120)
  check("rt_regn", r.regions[0].name == "OVLY1" and r.regions[0].end == 0x140)
  check("rt_phas", r.phaseRoot == "PHASE02" and r.phaseNumbers == [2, 3]
        and r.nocall and r.entryAddress == 0x110
        and r.entryName == "CODEAE1")
  check("rt_symbols", list(r.symbols())[0][0] == "CODEA")
  check("rt_occupied", r.occupiedIntervals() == [(0x100, 0x140)])
  check("rt_lsta", r.linkState == {"zconPoolNext": 0x494},
        f"{r.linkState}")


PROTDECK = """\
PDATA    CSECT
         SPON
         DC    X'AAAA'            protected constant
         DC    X'BBBB'            protected constant
         SPOFF
         DC    X'CCCC'            runtime-written
         ENTRY PDATA1
PDATA1   DC    X'DDDD'
         END
"""

PLAIN = """\
PCODE    CSECT
         EXTRN PDATA
         DC    Y(PDATA)
         DC    X'0700'
         END
"""


def integration(build):
  asm101 = build / "bin" / "asm101"
  python = build / "venv" / "bin" / "python"
  if not asm101.exists() or not python.exists():
    print("SKIP integration: build/bin/asm101 or venv missing")
    return

  with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td / "PROTDECK.asm").write_text(PROTDECK)
    (td / "PLAIN.asm").write_text(PLAIN)
    objs = []
    for stem in ("PROTDECK", "PLAIN"):
      obj = td / f"{stem}.obj"
      r = subprocess.run([str(asm101), "-o", str(obj),
                          str(td / f"{stem}.asm")],
                         capture_output=True, text=True, timeout=120)
      check(f"asm_{stem}", r.returncode == 0 and obj.exists(),
            (r.stdout + r.stderr)[-300:])
      objs.append(obj)

    lib = td / "out.lib"
    r = subprocess.run(
      [str(python), "-m", "lnk101", *map(str, objs),
       "-o", str(td / "out.fcm"), "--lib", str(lib), "--no-repro",
       "--no-default-libs", "--compact"],
      capture_output=True, text=True, timeout=120,
      cwd=str(HERE.parent))
    check("link_lib", r.returncode == 0 and lib.exists(),
          (r.stdout + r.stderr)[-400:])
    if not lib.exists():
      return
    m = LibModule.read(lib)

    syms = {name: (typ, addr) for name, typ, addr, _len in m.symbols()}
    check("lib_cesd_has_both",
          "PDATA" in syms and "PCODE" in syms and "PDATA1" in syms,
          f"{sorted(syms)}")
    check("lib_extents", len(m.extents) >= 1 and
          sum(x.hwLength for x in m.extents) >= 6)

    # Protection: PDATA is PROT-managed (hw 0-1 protected, 2-3 not);
    # PCODE has no marks -> class default for a CODE-named csect = protected.
    protAt = {}
    for x in m.extents:
      for i, p in enumerate(x.protect or []):
        protAt[x.address // 2 + i] = p
    pdata = syms["PDATA"][1] // 2
    pcode = syms["PCODE"][1] // 2
    check("lib_prot_managed",
          protAt.get(pdata) is True and protAt.get(pdata + 1) is True
          and protAt.get(pdata + 2) is False
          and protAt.get(pdata + 3) is False,
          f"PDATA hw prot: {[protAt.get(pdata + i) for i in range(4)]}")
    check("lib_prot_default_code",
          protAt.get(pcode) is True and protAt.get(pcode + 1) is True,
          f"PCODE hw prot: {[protAt.get(pcode + i) for i in range(2)]}")

    # The Y(PDATA) RLD survives with an absolute position inside PCODE and
    # resolves to PDATA's CESD id.
    ids = {e.name.strip(): e.esdId for e in m.cesd}
    hit = [r for r in m.rlds if r.relId == ids.get("PDATA")
           and r.posId == ids.get("PCODE")]
    check("lib_rld_retained", len(hit) == 1
          and hit[0].position == syms["PCODE"][1],
          f"rlds={[(r.flags, hex(r.position), r.relId, r.posId) for r in m.rlds]}")


SLIBDECK = """\
SLIB     CSECT
         ENTRY SLIBE1
         DC    X'C1C1'
SLIBE1   DC    X'C2C2'
         END
"""

PH2MAIN = """\
PMAIN    CSECT
         EXTRN SLIB
         DC    Y(SLIB)
         END
"""

# Column-1 label field carries the absolute halfword origin on OVERLAY cards.
PH1DECK = ("00100    OVERLAY SLIB\n"
           "         INSERT  SLIB\n"
           "         SET\n")
PH2DECK = ("         MAP     1,SLIB\n"
           "00200    OVERLAY MAIN\n"
           "         INSERT  PMAIN\n"
           "         SET\n")


def integration_map(build):
  """MAP phasing: phase 2 resolves a symbol from phase 1's .lib, places
  around its interval, and patches the Y-con with the mapped address."""
  asm101 = build / "bin" / "asm101"
  python = build / "venv" / "bin" / "python"
  if not asm101.exists() or not python.exists():
    print("SKIP map integration: build/bin/asm101 or venv missing")
    return

  with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    deck = td / "CON80"
    deck.mkdir()
    (deck / "PH1").write_text(PH1DECK)
    (deck / "PH2").write_text(PH2DECK)

    def run(name, args):
      r = subprocess.run(args, capture_output=True, text=True, timeout=120,
                         cwd=str(HERE.parent))
      check(name, r.returncode == 0, (r.stdout + r.stderr)[-400:])
      return r

    for stem, text in (("SLIB", SLIBDECK), ("PMAIN", PH2MAIN)):
      (td / f"{stem}.asm").write_text(text)
      run(f"map_asm_{stem}", [str(asm101), "-o", str(td / f"{stem}.obj"),
                              str(td / f"{stem}.asm")])

    run("map_link_ph1",
        [str(python), "-m", "lnk101", str(td / "SLIB.obj"),
         "-o", str(td / "ph1.fcm"), "--lib", str(td / "ph1.lib"),
         "--concard", str(deck), "--concard-root", "PH1",
         "--no-repro", "--no-default-libs"])
    ph1 = LibModule.read(td / "ph1.lib")
    check("map_ph1_region",
          any(r.name == "SLIB" and r.start == 0x200 for r in ph1.regions),
          f"{[(r.name, hex(r.start)) for r in ph1.regions]}")

    # Without the phase-1 lib the EXTRN SLIB is undefined and the link fails;
    # with --map-lib it resolves at the mapped address.
    r = subprocess.run(
      [str(python), "-m", "lnk101", str(td / "PMAIN.obj"),
       "-o", str(td / "ph2.fcm"), "--concard", str(deck),
       "--concard-root", "PH2", "--no-repro", "--no-default-libs"],
      capture_output=True, text=True, timeout=120, cwd=str(HERE.parent))
    check("map_link_ph2_fails_unmapped", r.returncode != 0,
          "link must fail without --map-lib (EXTRN SLIB unresolved)")

    run("map_link_ph2",
        [str(python), "-m", "lnk101", str(td / "PMAIN.obj"),
         "-o", str(td / "ph2.fcm"), "--lib", str(td / "ph2.lib"),
         "--concard", str(deck), "--concard-root", "PH2",
         "--map-lib", f"1={td / 'ph1.lib'}",
         "--no-repro", "--no-default-libs"])

    # PMAIN sits at hw 0x200 (byte 0x400); its Y(SLIB) must hold SLIB's
    # mapped halfword address 0x100.
    img = (td / "ph2.fcm").read_bytes()
    check("map_ycon_patched",
          len(img) >= 0x402 and img[0x400:0x402] == b"\x01\x00",
          f"image[0x400:0x402] = {img[0x400:0x402].hex() if len(img) >= 0x402 else 'short'}")
    ph2 = LibModule.read(td / "ph2.lib")
    names = {e.name.strip() for e in ph2.cesd}
    check("map_no_reexport", "PMAIN" in names and "SLIB" not in names,
          f"{sorted(names)}")


def phase_roots():
  """Virtual 'OFTMP@N' concard roots: the master deck's PHASE directives
  partition it into buildable phase segments (SKIPs without the OI34 tree)."""
  deckdir = HERE.parent / "code" / "OI340600" / "CON80"
  if not deckdir.is_dir():
    print("SKIP phase roots: code/OI340600/CON80 missing")
    return
  from ap101Utils import concard
  deck = concard.ConcardDeck(deckdir)
  segs = concard.phase_segments(deck, "OFTMP")
  ids = [nums[0] for nums, _ in segs if nums]
  check("phase_ids_unique", len(ids) == len(set(ids)), f"{ids}")
  check("phase_ssw_serves_all_ops",
        next((nums for nums, _ in segs if nums and nums[0] == 2), None)
        == [2, 3, 4, 5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 18])
  check("phase_root_has", deck.has("OFTMP@2") and deck.has("OFTMP@11")
        and not deck.has("OFTMP@99"))
  prog = concard.layout_program(deck, "OFTMP@3")
  maps = sorted({op.operand.split(",")[0] for op in prog
                 if op.verb == "MAP"})
  check("phase3_maps_1_and_2", maps == ["1", "2"], f"{maps}")
  # The prologue rides along; the segment's own PHASE card leads its body.
  check("phase_prologue",
        any(op.verb == "PHASE" and op.operand.startswith("3,")
            for op in prog))


def layout_phasing():
  """LayoutEngine phase support: sequential placement flows around occupied
  (MAP'd) intervals; an origin-less OVERLAY naming an earlier-phase region
  re-opens it there (replacement content)."""
  from ap101Utils.concard import LayoutOp
  from ap101Utils.conlayout import LayoutEngine
  mc = {n: [(n, 4)] for n in ("A", "B", "C")}
  prog = [LayoutOp("OVERLAY", "NEW", "T", 1),
          LayoutOp("INSERT", "A", "T", 2),      # lc 0 occupied -> lands at 8
          LayoutOp("OVERLAY", "OLD", "T", 3),   # re-open earlier region
          LayoutOp("INSERT", "B", "T", 4),
          LayoutOp("OVERLAY", "NOWHERE", "T", 5),
          LayoutOp("INSERT", "C", "T", 6)]      # continues past B's checksum
  p = LayoutEngine(mc, occupied=[(0, 8)],
                   phase_regions={"OLD": 0x40}).run(prog)
  check("layout_skips_occupied", p.addresses.get("A") == 8,
        f"A @ {p.addresses.get('A')}")
  check("layout_reopens_region", p.addresses.get("B") == 0x40,
        f"B @ {p.addresses.get('B')}")
  # OLD's block ends at 0x44 and gets the 2-hw inter-block checksum
  # reservation there; C flows past it.
  check("layout_unknown_overlay_continues", p.addresses.get("C") == 0x46,
        f"C @ {p.addresses.get('C')}")


def phase_resolution():
  """Cross-phase resolution: residual RLDs patch against sibling libs'
  CESDs (both directions), the ZCON unresolved marker is cleared before
  sector encoding, XRES marks the lib, and re-resolution is refused."""
  from lnk101.phaseresolve import resolvePhases

  def mklib(name, cesd, extents, rlds):
    m = LibModule(name=name)
    m.cesd, m.extents, m.rlds = cesd, extents, rlds
    return m

  # A @0x100: YCON site (addend 4 -> BSYM+4) and an unresolved-ZCON site
  # (marker 0x8000 | addend 2 -> BSYM+2, DSR untouched in sector 0).
  a = mklib("PHA", [EsdEntry.sd(1, "ASECT", 0x100, 0x10),
                    EsdEntry.ld(2, "ASYM", 0x108, 1),
                    EsdEntry.er(3, "BSYM"),
                    EsdEntry.er(4, "MISSING")],
            [Extent(0x100, bytes.fromhex("0000" "0004" "8002" "0000"
                                         "0000" "0000" "0000" "0000"))],
            [LibRld(0x00, 0x102, 3, 1),      # YCON -> BSYM
             LibRld(0x50, 0x104, 3, 1),      # ZCON/data -> BSYM (4 bytes)
             LibRld(0x00, 0x10A, 4, 1)])     # YCON -> MISSING (stays)
  # B @0x400 defines BSYM (LD at 0x404) and references ASYM back into A.
  b = mklib("PHB", [EsdEntry.sd(1, "BSECT", 0x400, 0x08),
                    EsdEntry.ld(2, "BSYM", 0x404, 1),
                    EsdEntry.er(3, "ASYM")],
            [Extent(0x400, bytes.fromhex("0000" "0000" "0000" "0000"))],
            [LibRld(0x00, 0x402, 3, 1)])     # YCON -> ASYM

  with tempfile.TemporaryDirectory() as td:
    pa, pb = Path(td) / "PHA.lib", Path(td) / "PHB.lib"
    a.write(pa)
    b.write(pb)

    dry = resolvePhases([pa, pb], dryRun=True)
    check("xres_dry_counts", dry.patched == {"PHA": 2, "PHB": 1}
          and dry.remaining["PHA"] == {"MISSING": 1}, f"{dry.patched}")
    check("xres_dry_no_write", not LibModule.read(pa).crossResolved)

    res = resolvePhases([pa, pb])
    check("xres_no_errors", not res.errors, f"{res.errors}")
    ra, rb = LibModule.read(pa), LibModule.read(pb)
    da, db = ra.extents[0].data, rb.extents[0].data
    # BSYM = byte 0x404 = hw 0x202: YCON = 0x202 + 4; ZCON hw0 = marker
    # cleared, 0x8000|0x202 + 2 addend.
    check("xres_ycon", da[2:4] == bytes.fromhex("0206"), da.hex())
    check("xres_zcon", da[4:8] == bytes.fromhex("8204" "0000"), da.hex())
    check("xres_missing_untouched", da[10:12] == b"\x00\x00")
    # ASYM = byte 0x108 = hw 0x084.
    check("xres_backref", db[2:4] == bytes.fromhex("0084"), db.hex())
    check("xres_marker", ra.crossResolved and ra.xresCount == 2
          and rb.crossResolved and rb.xresCount == 1)

    again = resolvePhases([pa, pb])
    check("xres_refused", len(again.errors) == 2, f"{again.errors}")


HOLEDECK = """\
HALHOLE  CSECT
         DC    X'AAAA'
         DS    2H
         DC    X'BBBB'
         END
"""


def hal_hole_fill(build):
  """The flight link editor fills object-text HOLES of HAL/S-translated
  csects with EBCDIC "FF" halfwords (C6C6) -- every DASS alignment-gap
  site inside a TXT-bearing #P/#D csect holds C6C6 in the OI340700 load
  module.  Producer identity comes from the END-card IDR (translator
  'HAL/S...'); assembler objects (blank IDR) keep zero-filled holes."""
  asm101 = build / "bin" / "asm101"
  python = build / "venv" / "bin" / "python"
  if not asm101.exists() or not python.exists():
    print("SKIP hal_hole_fill: build/bin/asm101 or venv missing")
    return

  with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td / "HOLE.asm").write_text(HOLEDECK)
    obj = td / "HOLE.obj"
    r = subprocess.run([str(asm101), "-o", str(obj), str(td / "HOLE.asm")],
                       capture_output=True, text=True, timeout=120)
    check("hole_asm", r.returncode == 0 and obj.exists(),
          (r.stdout + r.stderr)[-300:])
    if not obj.exists():
      return
    # asm101 TXT-covers the DS interior with zeros (one contiguous record),
    # so manufacture a real HOLE: split the TXT card into bytes 0-1 and 6-7.
    # Then a HAL-translated twin: same cards, END IDR flagged type 2 with
    # the compiler's translator identification (cols 33-52).
    from ap101Utils.objModule import asciiToEbcdicBytes
    cards = []
    for i in range(0, len(obj.read_bytes()), 80):
      card = bytearray(obj.read_bytes()[i:i + 80])
      kind = card[1:4].decode("cp037", errors="replace")
      if kind == "TXT":
        t2 = bytearray(card)
        card[5:8] = (0).to_bytes(3, "big")
        card[10:12] = (2).to_bytes(2, "big")
        card[16:18] = b"\xAA\xAA"
        t2[5:8] = (6).to_bytes(3, "big")
        t2[10:12] = (2).to_bytes(2, "big")
        t2[16:18] = b"\xBB\xBB"
        cards += [bytes(card), bytes(t2)]
      else:
        cards.append(bytes(card))
    (td / "HOLE.obj").write_bytes(b"".join(cards))
    halcards = []
    for card in cards:
      c = bytearray(card)
      if c[1:4].decode("cp037", errors="replace") == "END":
        c[32] = 0xF2
        c[33:52] = asciiToEbcdicBytes("HAL/SREL3 V0  26199", 19)
      halcards.append(bytes(c))
    (td / "HOLEHAL.obj").write_bytes(b"".join(halcards))

    for stem, expectFill in (("HOLE", False), ("HOLEHAL", True)):
      fcm = td / f"{stem}.fcm"
      r = subprocess.run(
        [str(python), "-m", "lnk101", str(td / f"{stem}.obj"),
         "-o", str(fcm), "--no-repro", "--no-default-libs", "--compact"],
        capture_output=True, text=True, timeout=120, cwd=str(HERE.parent))
      check(f"hole_link_{stem}", r.returncode == 0 and fcm.exists(),
            (r.stdout + r.stderr)[-400:])
      if not fcm.exists():
        continue
      img = fcm.read_bytes()
      want = (b"\xAA\xAA\xC6\xC6\xC6\xC6\xBB\xBB" if expectFill
              else b"\xAA\xAA\x00\x00\x00\x00\xBB\xBB")
      check(f"hole_fill_{stem}", want in img,
            f"expected {want.hex()} in image; csect bytes: "
            + img[:64].hex())


def main():
  unit_roundtrip()
  layout_phasing()
  phase_resolution()
  integration(HERE.parent / "build")
  integration_map(HERE.parent / "build")
  phase_roots()
  hal_hole_fill(HERE.parent / "build")
  if _failures:
    print(f"FAILED {len(_failures)} libModule check(s):")
    for f in _failures:
      print(f"  - {f}")
    return 1
  print(f"OK: all {_passes} libModule checks passed")
  return 0


if __name__ == "__main__":
  sys.exit(main())
