"""HAL/S-FC optimizer common-subexpression-elimination regression test.

Guards the XCOM-I `BASED ... RECORD DYNAMIC` row-stride fix (2026-07-17):
the OPT pass keeps its CSE catalog in dynamic-width records (PAR_SYM /
VAL_TABLE, row width set at runtime by SET_RECORD_WIDTH).  XCOM-I used to
address them with the static one-element stride, so PUSH_STACK's
level-to-level catalog copy aliased and smeared the tables at every
IF/DO-group entry — the optimizer could then only find CSEs that never
crossed a nested-block boundary.

Two probes, compiled with the X6 (statistics) option and judged on the
"CSE'S FOUND" counter in opt.rpt:

  * same-level:  X = A$(I)+1; Y = A$(I)+2;          -- worked all along
  * cross-block: IF A$(I) ^= 2 THEN DO; X = A$(I);  -- the regression

Usage:  test_halsfc_cse.py [path-to-halsc [workdir]]
"""
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SAME_LEVEL = """ CSE1: PROGRAM;
    DECLARE A ARRAY(10) INTEGER INITIAL(0);
    DECLARE I INTEGER INITIAL(1);
    DECLARE X INTEGER INITIAL(0);
    DECLARE Y INTEGER INITIAL(0);
    X = A$(I) + 1;
    Y = A$(I) + 2;
 CLOSE CSE1;
"""

CROSS_BLOCK = """ CSE2: PROGRAM;
    DECLARE A ARRAY(10) INTEGER INITIAL(0);
    DECLARE I INTEGER INITIAL(1);
    DECLARE X INTEGER INITIAL(0);
    IF A$(I) NOT = 2 THEN DO;
       X = A$(I);
    END;
 CLOSE CSE2;
"""

PARM = "SREF,LIST,SRN,NOLFXI,REGOPT,X6,CARDTYPE=FCRMUDXCVMWC"


def cses_found(halsc, workroot, name, source):
    """Compile one probe, return the optimizer's CSE'S FOUND count."""
    srcpath = os.path.join(workroot, name + ".hal")
    workdir = os.path.join(workroot, name + ".work")
    objpath = os.path.join(workroot, name + ".obj")
    with open(srcpath, "w") as f:
        f.write(source)
    r = subprocess.run(
        [halsc, "--parm=" + PARM, "--workdir=" + workdir,
         "-o", objpath, srcpath],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0:
        print("FAIL: halsc rc=%d for %s" % (r.returncode, name))
        print(r.stdout[-2000:])
        return None
    optrpt = os.path.join(workdir, "opt.rpt")
    if not os.path.exists(optrpt):
        print("FAIL: no opt.rpt for %s" % name)
        return None
    text = open(optrpt, errors="replace").read()
    m = re.search(r"CSE'S FOUND\s*=\s*(\d+)", text)
    if not m:
        print("FAIL: no CSE'S FOUND line in opt.rpt for %s" % name)
        return None
    return int(m.group(1))


def main():
    halsc = sys.argv[1] if len(sys.argv) > 1 else \
        os.path.join(REPO, "build", "bin", "halsc")
    if not os.path.exists(halsc):
        print("SKIP: %s not found" % halsc)
        return 0
    workroot = sys.argv[2] if len(sys.argv) > 2 else \
        os.path.join(REPO, "build", "test", "halsfc_cse")
    os.makedirs(workroot, exist_ok=True)

    failures = 0
    for name, source, why in (
            ("cse_same_level", SAME_LEVEL,
             "CSE within one nesting level"),
            ("cse_cross_block", CROSS_BLOCK,
             "CSE across an IF...THEN DO boundary "
             "(XCOM-I RECORD DYNAMIC stride regression)")):
        n = cses_found(halsc, workroot, name, source)
        if n is None:
            failures += 1
        elif n < 1:
            print("FAIL: %s: optimizer found %d CSEs, expected >= 1 (%s)"
                  % (name, n, why))
            failures += 1
        else:
            print("ok: %s: %d CSE(s) found" % (name, n))

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
