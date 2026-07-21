"""Every-deck sweep for the dfg tool.

Discovers every display deck in a test tree and runs on each.

  * No deck may crash: every deck either generates or throws
    dfg.model.Error
  * Every PADR type-resolves against the SDF library (no NAME BIT(16)
    fallback): the compool resolver reads ONLY the compiler's PASS3 SDFs,
    and with the build's SDFLIB present every referent must be found there.

DFG_SDFLIB points the resolver at a different SDF library (default: the
OI340600 build's gen/SDFLIB).

"""
import glob
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.join(REPO, "..", "pfs", "code", "OI340600")
sys.path.insert(0, os.path.join(REPO, "src"))

from dfg.encode import encode, Error
from dfg.emit import to_hal, n_untyped


def diagnostics_regression():
    """Missing-SDF refusal regression (self-contained: builds its own SDF
    libraries from the `test/data/dfg_sdflib` fixtures — CG0250's four
    INCLUDEd compools compiled by halsc — and restores the default after).

    Build-survey history: display decks whose INCLUDEd compools had no SDF
    member failed with messages that read like deck-syntax bugs —
    `cannot derive rate count: -- RATE = 3` (CG0250/CG0340/CG2011, RTC
    string table in the uncompiled CGZFL2) and a wrapped multi-line
    `cannot derive condition word: RTC = (...` (the CS* SPEC decks,
    uncompiled CSAPDT).  The decks were fine.  Gate that:
      * with every INCLUDE's SDF present, CG0250 generates (typed);
      * without CGZFL2's SDF only, the refusal names the blocking RTC and
        its string table, on one line;
      * the missing-SDF cause is identifiable
        (`resolve.missing_sdf_includes`, the CLI's
        `[no SDF member for INCLUDE(s): ...]` hint);
      * condition-word refusals stay one line even for a directive
        continued across cards (CS1680's BLT=)."""
    import shutil
    from dfg import cli, compool
    from dfg.deck import encodable_directives
    from dfg.resolve import char_inits, missing_sdf_includes

    problems = []

    def check(cond, what):
        if not cond:
            problems.append("diagnostics: " + what)

    fixture = os.path.join(REPO, "test", "data", "dfg_sdflib")
    if not os.path.isdir(fixture):
        return ["diagnostics: fixture %s missing" % fixture]
    tmp = tempfile.mkdtemp(prefix="dfg_sdflib_")
    reduced = os.path.join(tmp, "no_cgzfl2")     # all fixtures but CGZFL2
    os.mkdir(reduced)
    for f in os.listdir(fixture):
        if f != "##CGZFL2.sdf":
            shutil.copy(os.path.join(fixture, f), reduced)
    empty = os.path.join(tmp, "empty")
    os.mkdir(empty)
    try:
        # (1) Full fixture lib: CG0250 (RM ORBIT) generates, fully typed.
        compool.set_sdflib(fixture)
        ds = encodable_directives("CG0250")
        check(missing_sdf_includes(ds) == [],
              "fixture lib reports missing includes: %s"
              % missing_sdf_includes(ds))
        # The RTC string-table vars are visible (presence gates the draw).
        ci = char_inits(ds)
        check(ci.get("CGZV_BLANK_ASTER") is not None
              and ci.get("CGZV_ASTER_BLANK") is not None,
              "char_inits does not surface the CGZFL2 string tables")
        try:
            enc = encode("CG0250")
            to_hal(enc)
            check(n_untyped(enc) == 0,
                  "CG0250: %d PADR(s) untyped on the fixture lib"
                  % n_untyped(enc))
        except Error as e:
            check(False, "CG0250 refused on the fixture lib: %s" % e)

        # (2) CGZFL2's SDF withheld: the phase-5 build failure, verbatim —
        # the refusal must name the blocking RTC and stay on one line.
        compool.set_sdflib(reduced)
        try:
            encode("CG0250")
            check(False, "CG0250 encoded without CGZFL2's SDF")
        except Error as e:
            msg = str(e)
            check("cannot derive rate count" in msg, "CG0250: %s" % msg)
            check("RTC" in msg and "CGZV_BLANK_ASTER" in msg,
                  "CG0250 refusal does not name the blocking RTC: %s" % msg)
            check("\n" not in msg, "CG0250 refusal is multi-line")
        check(missing_sdf_includes(ds) == ["CGZFL2"],
              "missing includes != [CGZFL2]: %s" % missing_sdf_includes(ds))
        check("CGZFL2" in cli._sdf_hint("CG0250"),
              "CLI hint does not name CGZFL2: %r" % cli._sdf_hint("CG0250"))

        # (3) Empty lib: condition-word refusals (the CS* SPEC family) are
        # one line even when the directive value spans continuation cards.
        compool.set_sdflib(empty)
        for name in ("CS1670", "CS1680"):
            try:
                encode(name)
                check(False, "%s encoded on an empty SDFLIB" % name)
            except Error as e:
                msg = str(e)
                check("cannot derive condition word" in msg,
                      "%s: %s" % (name, msg))
                check("\n" not in msg,
                      "%s refusal is multi-line: %r" % (name, msg))
        missing = missing_sdf_includes(encodable_directives("CS1670"))
        check("CSA_PDT" in missing,
              "CS1670 missing-SDF includes lack CSA_PDT: %s" % missing)
    finally:
        compool.set_sdflib(None)                 # back to DFG_SDFLIB/default
        shutil.rmtree(tmp, ignore_errors=True)
    return problems


def amt_regression():
    """AMT-mode (CDAPnn moding-table) generation regression, self-contained
    on checked-in fixtures (test/data/amt): OI34 input decks CDAP04 (AMTG,
    two OPS groups, full 12-entry SPEC table) and CDAP16 (AMTS, CC=YES
    payload-command-filter SPECs, SM CDAV_MF row) with golden generated
    compool text.  The generation rules were validated element-for-element
    against every OI301700.listing CDAPnn oracle (DFG VERSION 30.40); the
    goldens freeze that behavior for our decks.  Also gates:
      * deck-form sniffing (dfg.amt.is_amt_deck);
      * con80build.classify routing AMT decks to 'AMT' while the hand-built
        CDAP02-style HAL member stays 'HAL';
      * template_names (the hal() closure seeds): DFB/DMMD + OPGM + SPGM."""
    from dfg import amt
    from ap101Utils.con80build import classify

    fixture = os.path.join(REPO, "test", "data", "amt")
    if not os.path.isdir(fixture):
        return ["amt: fixture %s missing" % fixture]
    problems = []

    def check(cond, what):
        if not cond:
            problems.append("amt: " + what)

    for name in ("CDAP04", "CDAP16"):
        deck = os.path.join(fixture, name)
        golden = deck + ".hal"
        check(amt.is_amt_deck(deck), "%s not sniffed as an AMT deck" % name)
        check(classify(__import__("pathlib").Path(deck)) == "AMT",
              "%s not classified AMT" % name)
        try:
            text = amt.generate(deck)
        except amt.AmtError as e:
            problems.append("amt: %s failed to generate: %s" % (name, e))
            continue
        want = open(golden, errors="replace").read()
        if text != want:
            import difflib
            d = list(difflib.unified_diff(want.splitlines(), text.splitlines(),
                                          "golden", "generated", lineterm=""))
            problems.append("amt: %s differs from golden (%d diff lines):\n"
                            "      %s" % (name, len(d),
                                          "\n      ".join(d[:12])))

    # closure seeds: DFB/DMMD names derived from PMF + control-segment pgms
    try:
        d4 = amt.parse(os.path.join(fixture, "CDAP04"))
        seeds = amt.template_names(d4)
        check(seeds[:6] == ["CDB021", "CDD032", "CDF043",
                            "CDG044", "CDH045", "CDR04D"],
              "CDAP04 DFB seeds wrong: %s" % seeds[:6])
        check("GO1_ASCENT_OPS" in seeds and "GUK_BRG_CS" in seeds,
              "CDAP04 program seeds missing: %s" % seeds)
    except amt.AmtError as e:
        problems.append("amt: CDAP04 parse failed: %s" % e)

    # a display deck / hand-built HAL member must not sniff as AMT
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".hal", delete=False) as f:
        f.write("C THIS OFT AMT BUILT BY HAND FOR REMOTE DFB'S\n"
                "D INCLUDE TEMPLATE CDB021      REMOTE NOLIST\n"
                " CDA_P02_AMT:  COMPOOL RIGID;\n CLOSE;\n")
        hand = f.name
    try:
        check(not amt.is_amt_deck(hand), "hand-built HAL sniffed as AMT")
        check(classify(__import__("pathlib").Path(hand)) == "HAL",
              "hand-built CDAP02-style member not classified HAL")
    finally:
        os.unlink(hand)
    return problems


def main():
    amt_problems = amt_regression()
    if not os.path.isdir(ROOT):
        if amt_problems:
            print("\n%d problem(s):" % len(amt_problems))
            for p in amt_problems:
                print("  " + p)
            return 1
        print("SKIP deck sweep: %s not found (amt regression PASS)" % ROOT)
        return 0
    os.environ["DFG_DECK_ROOT"] = ROOT
    diag_problems = amt_problems + diagnostics_regression()
    # C* = display decks, X* = critical-format (CRTFMT) background decks.
    # XD0000 is excluded: it is a hand-written .hal FCW array, not a deck.
    names = sorted(os.path.basename(p) for d in ("SSSRC", "APPLSRC")
                   for p in glob.glob(os.path.join(
                       ROOT, d, "[CX][DGSV][0-9][0-9][0-9][0-9]"))
                   if os.path.basename(p) != "XD0000")
    problems = []
    n_ok = 0
    refused = {}
    for name in names:
        try:
            enc = encode(name)
            to_hal(enc)
        except Error as e:
            refused[name] = str(e)
            continue
        except Exception as e:
            problems.append("%s: ERROR %s: %s" % (name, type(e).__name__, e))
            continue
        n_ok += 1
        u = n_untyped(enc)
        if u:
            problems.append("%s: %d PADR(s) did not type-resolve on SDFLIB"
                            % (name, u))

    for name, msg in sorted(refused.items()):
        problems.append("%s: refusal (%s)" % (name, msg))

    problems = diag_problems + problems
    if problems:
        print("\n%d problem(s):" % len(problems))
        for p in problems:
            print("  " + p)
        return 1
    print("ALL PASS (%d decks: %d generated, %d refused)"
          % (len(names), n_ok, len(refused)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
