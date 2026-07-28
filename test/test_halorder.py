#!/usr/bin/env python3
#
# ap101Utils.halorder regression tests (template closure inputs):
#   1. parse_hal reads D INCLUDE TEMPLATE cards with AND without a blank
#      after the column-1 card-type 'D' (OI340600 punches both; the
#      no-blank form hid GEHRTL's producers from the closure).
#   2. display_deps extracts a DFG display deck's INCLUDE= compool
#      requirements (full or pre-descored names, columns 1-2 only).
#   3. template_closure(seed_names=...) admits display-seeded producers
#      transitively, segregating PROGRAM producers into the seed set.
#   4. Variant decks (per-file _CARDTYPE entries) parse with the parm's
#      change-flag map: inactive variant headers (A->C on GKRORB) are
#      skipped, the active one (O->M) wins, flag->D includes count
#      (G->D on GKRORB, B->D on CPTOSV) and flag->C ones do not; files
#      without an entry keep the standard card types.
#   5. get_parms maps the 'T' change-flag card type (CARDTYPE=...TM);
#      unmapped, PASS1 comments those cards out and the unit abends
#      (OI340600 APPLSRC/GKFHOR).
#
# Run standalone:  build/venv/bin/python test/test_halorder.py
# or via ctest:    ctest -R halorder
#
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from ap101Utils import halorder

_failures = []
_passes = 0


def check(name, cond, detail=""):
  global _passes
  if cond:
    _passes += 1
  else:
    _failures.append(f"{name}: {detail}" if detail else name)


def _write(d: Path, name: str, text: str) -> Path:
  p = d / name
  p.write_text(text)
  return p


def include_card_forms():
  """D-space and D-nospace INCLUDE TEMPLATE cards both parse."""
  with tempfile.TemporaryDirectory() as td:
    p = _write(Path(td), "UNITA", "\n".join([
        "D INCLUDE TEMPLATE CGN_MC16 NOLIST",
        "DINCLUDE TEMPLATE GVN_COVINIT NOLIST",
        "D  INCLUDE TEMPLATE GKC_ASC_RTLS_KIP",
        "C DINCLUDE TEMPLATE NOT_A_DEP (comment card)",
        " UNIT_A: PROCEDURE;",
        " CLOSE UNIT_A;",
        "",
    ]))
    name, deps = halorder.parse_hal(p)
  check("nospace_name", name == "UNITA", f"{name}")
  check("nospace_deps", deps == ["CGNMC1", "GVNCOV", "GKCASC"], f"{deps}")


def display_deck_deps():
  """INCLUDE= cards yield descored names; comments/deep columns don't."""
  with tempfile.TemporaryDirectory() as td:
    p = _write(Path(td), "CG9999", "\n".join([
        " HEADER=9999G,",
        " INCLUDE=CGZ_FLD_MC_16_3,",
        "INCLUDE=CGSGNC,",
        " INCLUDE=CGZ_FLD_MC_16_3,",
        " */  INCLUDE=NOTREAL - comment text,",
        "         SOMEOP,INCLUDE=X, deep operand field must not match",
        " STAT,",
    ]))
    deps = halorder.display_deps(p)
  check("disp_deps", deps == ["CGZFLD", "CGSGNC"], f"{deps}")


def closure_seed_names():
  """Seeded names close transitively; PROGRAM producers go to seed set."""
  with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    # worklist unit, references nothing
    work = _write(d, "WRK", " WRK_UNIT: PROCEDURE;\n CLOSE WRK_UNIT;\n")
    # display-seeded compool A includes compool B (transitive), and a PROGRAM
    _write(d, "CPA", "\n".join([
        "DINCLUDE TEMPLATE CP_B_COMPOOL",
        "D INCLUDE TEMPLATE PRG_MAIN",
        " CP_A_COMPOOL: COMPOOL;",
        " CLOSE CP_A_COMPOOL;",
    ]))
    _write(d, "CPB", " CP_B_COMPOOL: COMPOOL;\n CLOSE CP_B_COMPOOL;\n")
    _write(d, "PRG", " PRG_MAIN: PROGRAM;\n CLOSE PRG_MAIN;\n")
    tree = halorder.tree_units([d])
    extra, seeds = halorder.template_closure([work], tree,
                                             seed_names=["CPACOM"])
    names = [p.name for p in extra]
  check("seed_transitive", names == ["CPA", "CPB"], f"{names}")
  check("seed_program", seeds == {"PRGMAI"}, f"{seeds}")
  # without seeds nothing is pulled
  with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    work = _write(d, "WRK", " WRK_UNIT: PROCEDURE;\n CLOSE WRK_UNIT;\n")
    extra, seeds = halorder.template_closure([work],
                                             halorder.tree_units([d]))
  check("seed_default_empty", (extra, seeds) == ([], set()),
        f"{extra} {seeds}")


def cardtype_variant_decks():
  """Variant decks (per-file _CARDTYPE) parse with the parm's flag map.

  Fixture files are NAMED like the real variant decks so _cardtype_map
  finds their _CARDTYPE entries; content is synthetic.  GKRORB's map:
  A->C (inactive variant), O->M (active), N->C, F->C, G->D."""
  content = "\n".join([
      "A   WRONG_UNIT: PROCEDURE;",
      "O   RIGHT_UNIT: PROCEDURE;",
      "N   OTHER_UNIT: PROCEDURE;",
      "D INCLUDE TEMPLATE REAL_DEP",
      "GINCLUDE TEMPLATE G_FLAGGED",
      "FINCLUDE TEMPLATE F_FLAGGED",
      " CLOSE RIGHT_UNIT;",
      "",
  ])
  with tempfile.TemporaryDirectory() as td:
    variant = _write(Path(td), "GKRORB", content)
    name, deps = halorder.parse_hal(variant)
    check("variant_header", name == "RIGHTU", f"{name}")
    check("variant_kind",
          halorder.unit_header(variant) == ("RIGHT_UNIT", "PROCEDURE"),
          f"{halorder.unit_header(variant)}")
    # G->D counts as an include; F->C does not
    check("variant_deps", deps == ["REALDE", "GFLAGG"], f"{deps}")
    # a non-variant file with the same content keeps the old behavior:
    # first header-looking line wins, flagged includes don't count
    plain = _write(Path(td), "PLAINX", content)
    pname, pdeps = halorder.parse_hal(plain)
    check("plain_header", pname == "WRONGU", f"{pname}")
    check("plain_deps", pdeps == ["REALDE"], f"{pdeps}")


def cardtype_b_include():
  """CPTOSV-style B->D: a B-flagged INCLUDE TEMPLATE card is live; the
  A-flagged one (A->C) is not."""
  with tempfile.TemporaryDirectory() as td:
    p = _write(Path(td), "CPTOSV", "\n".join([
        "D INCLUDE TEMPLATE CSA_PDT",
        "B INCLUDE TEMPLATE CS4_PDT",
        "A INCLUDE TEMPLATE NOT_LIVE",
        " CPT_OSV: COMPOOL RIGID;",
        " CLOSE CPT_OSV;",
        "",
    ]))
    name, deps = halorder.parse_hal(p)
  check("b_include_name", name == "CPTOSV", f"{name}")
  check("b_include_deps", deps == ["CSAPDT", "CS4PDT"], f"{deps}")


def cardtype_t_mapped():
  """Default parm maps the 'T' change flag (and the historical set)."""
  parm = halorder.get_parms("GKFHOR")
  ct = parm.split("CARDTYPE=")[1].split(",")[0]
  pairs = dict(zip(ct[0::2], ct[1::2]))
  check("cardtype_pairs_even", len(ct) % 2 == 0, ct)
  check("cardtype_T", pairs.get("T") == "M", f"{pairs}")
  for c in "FRUXVW":
    check(f"cardtype_{c}", c in pairs, f"{pairs}")


def main():
  include_card_forms()
  display_deck_deps()
  closure_seed_names()
  cardtype_variant_decks()
  cardtype_b_include()
  cardtype_t_mapped()
  if _failures:
    print(f"FAILED {len(_failures)} halorder check(s):")
    for f in _failures:
      print(f"  - {f}")
    return 1
  print(f"OK: all {_passes} halorder checks passed")
  return 0


if __name__ == "__main__":
  sys.exit(main())
