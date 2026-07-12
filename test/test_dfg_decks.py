"""Every-deck sweep for the dfg tool.

Discovers every display deck in a test tree and runs on each.  

  * No deck may crash: every deck either generates or throws
    dfg.model.Error
  * Every PADR type-resolves against TEMPLIB (no NAME BIT(16) fallback):
    the compool resolver reads ONLY the compiled templates, and with the
    build's TEMPLIB present every referent must be found there.

"""
import glob
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.join(REPO, "..", "pfs", "code", "OI340600")
sys.path.insert(0, os.path.join(REPO, "src"))

from dfg.encode import encode, Error
from dfg.emit import to_hal, n_untyped


def main():
    if not os.path.isdir(ROOT):
        print("SKIP: %s not found" % ROOT)
        return 0
    os.environ["DFG_DECK_ROOT"] = ROOT
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
            problems.append("%s: %d PADR(s) did not type-resolve on TEMPLIB"
                            % (name, u))

    for name, msg in sorted(refused.items()):
        problems.append("%s: refusal (%s)" % (name, msg))


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
