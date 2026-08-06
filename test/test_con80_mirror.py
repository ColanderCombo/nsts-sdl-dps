#!/usr/bin/env python3
"""con80build .hal-mirror build-layer rewrites.

Covers:
  1. _native_comment_chars: only per-file CARDTYPE pairs that remap a
     NATIVE type char (E/M/S/C/D) to comment count — change-flag pairs
     (GKRORB A->C) and default decks yield nothing.  No live table entry
     exists today (CV5SLCOM "DC" was retired), so the materialization
     checks inject a synthetic entry.
  2. comment_native_cards mechanics: col-1-only rewrite, columns/SRN
     preserved, None when nothing matches.
  3. _hal_mirror materialization: a deck with a native remap becomes a
     transformed copy; everything else stays a symlink; _mirror_rewrite
     agrees with the materialized bytes.
  4. halorder CARDTYPE table (flight-faithful STRPDT dispositions):
     default maps R->C (the SM compools compile INCL80/STRPDT's
     unqualified NAME declares as comments, so their SDFs export only
     the structure templates and multi-import works), while the 7
     flight-verified procedures map R->M.
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from ap101Utils import halorder
from con80.con80build import (_hal_mirror, _mirror_rewrite,
                                   _native_comment_chars,
                                   comment_native_cards)

_failures = []
_passes = 0


def check(name, cond, detail=""):
  global _passes
  if cond:
    _passes += 1
  else:
    _failures.append((name, detail))
    print(f"FAIL {name}: {detail}")


# 72-column card body + SRN field, CRLF line ends like the OI source tree.
def card(text, srn="000100AA"):
  return f"{text:<72s}{srn}\r\n"


NATIVE_REMAP = card("D INCLUDE TEMPLATE CPS_SLD NOLIST") \
    + card(" ZZ_NATIVE: COMPOOL RIGID;") \
    + card(" REPLACE CPSB_GTOD_XFER BY \"CV5B_DUMMY2\";") \
    + card(" CLOSE ZZ_NATIVE;")

PLAIN = card(" GKF_HOR: PROCEDURE;") + card(" CLOSE GKF_HOR;")


def test_native_remap():
  check("no live native remap in the table",
        all(_native_comment_chars(stem) == ""
            for stem in list(halorder._CARDTYPE) if stem != "default"))
  check("change-flag remaps (GKRORB A->C) are not native",
        _native_comment_chars("GKRORB") == "")
  check("default decks have no native remap",
        _native_comment_chars("GKFHOR") == "")

  halorder._CARDTYPE["ZZNATIVE"] = "DC"
  try:
    check("synthetic DC entry detected as native D",
          _native_comment_chars("ZZNATIVE") == "D")

    t = comment_native_cards(NATIVE_REMAP, "D")
    check("native remap rewrote something", t is not None)
    lines, orig = t.splitlines(True), NATIVE_REMAP.splitlines(True)
    check("native remap keeps card count", len(lines) == len(orig))
    diffs = [(a, b) for a, b in zip(orig, lines) if a != b]
    check("only the D card touched", len(diffs) == 1, diffs)
    a, b = diffs[0]
    check("native remap col-1 D->C only",
          a[0] == "D" and b[0] == "C" and a[1:] == b[1:], (a, b))
    check("no-match returns None", comment_native_cards(PLAIN, "D") is None)

    with tempfile.TemporaryDirectory() as td:
      src = Path(td) / "APPLSRC"
      src.mkdir()
      (src / "ZZNATIVE").write_bytes(NATIVE_REMAP.encode("latin-1"))
      (src / "GKFHOR").write_bytes(PLAIN.encode("latin-1"))
      haltree = Path(td) / "haltree"
      _hal_mirror([src], haltree)
      m = haltree / "APPLSRC"
      check("native-remap deck materialized",
            (m / "ZZNATIVE.hal").is_file()
            and not (m / "ZZNATIVE.hal").is_symlink())
      check("materialized content is the remap text",
            (m / "ZZNATIVE.hal").read_bytes() == t.encode("latin-1"))
      check("other decks still symlinked", (m / "GKFHOR.hal").is_symlink())
      check("_mirror_rewrite matches", _mirror_rewrite(src / "ZZNATIVE") == t)
      # Re-mirror: materialized copy not rewritten when unchanged.
      before = (m / "ZZNATIVE.hal").stat().st_mtime_ns
      _hal_mirror([src], haltree)
      check("re-mirror leaves unchanged copy alone",
            (m / "ZZNATIVE.hal").stat().st_mtime_ns == before)
  finally:
    halorder._CARDTYPE.pop("ZZNATIVE", None)

  check("pristine deck needs no rewrite",
        _mirror_rewrite(Path("/nonexistent-has-no-entry")) is None)


def test_cardtype_table():
  # Flight-faithful STRPDT dispositions (OI30 listings): compools R->C...
  for stem in ("CSAPDT", "CS2PDT", "CS4PDT", "CPGPCD", "PGGPCF",
               "PGPPLD", "PGSCRU"):
    check(f"{stem} parm maps R->C",
          "CARDTYPE=FCRC" in halorder.get_parms(stem),
          halorder.get_parms(stem))
  # ...and the 8 procedures whose listings compile the R cards live R->M.
  for stem in ("SAFACQ", "SCKPNT", "STCCYCL", "SULUPLIN", "STMTAB",
               "SPNINT", "SPPPRECO", "SRESTO"):
    check(f"{stem} parm maps R->M",
          "CARDTYPE=FCRM" in halorder.get_parms(stem),
          halorder.get_parms(stem))
  # CPTOSV/CPUSLS: deliberate both-OPS config (B->D) but flight R->C.
  for stem in ("CPTOSV", "CPUSLS"):
    parm = halorder.get_parms(stem)
    check(f"{stem} keeps B->D both-OPS with R->C",
          "ACBDFCRC" in parm, parm)


def main():
  test_native_remap()
  test_cardtype_table()
  print(f"{_passes} passed, {len(_failures)} failed")
  return 1 if _failures else 0


if __name__ == "__main__":
  sys.exit(main())
