#!/usr/bin/env python3
#
# ibmobjdump --normalize: the canonical form used to compare two object files.
#
# WHY THE SWITCH EXISTS.  An assembler may emit its ESD entries in any order.
# ASM101S keeps ENTRY and EXTRN symbols in Python sets, so the order varies
# from run to run: assembling one module three times produced three different
# files, 411 of 3280 bytes moving, with no difference in meaning.  Comparing
# such objects byte-for-byte answers the wrong question.  Nothing else in the
# file is positional -- TXT, RLD and END all name their section -- so dropping
# the ESD index columns and sorting is lossless.

import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
FIXTURE = HERE / "asm101" / "golden" / "scal_with_base_emits_rld.obj"


def dump(*args):
    r = subprocess.run(
        [sys.executable, "-m", "tools.objdump", "--no-repro", *args, str(FIXTURE)],
        capture_output=True, text=True, cwd=str(SRC), timeout=120)
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture object not present")
def test_normalize_drops_the_positional_columns():
    """The ESD index is where an entry landed, not part of what it says."""
    for line in dump("--normalize"):
        if line.startswith(("ESD", "TXT")):
            assert not re.match(r"^(ESD|TXT)\s+\[\s*\d+\]", line), line


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture object not present")
def test_normalize_sorts_the_esd():
    esd = [l for l in dump("--normalize") if l.startswith("ESD")]
    assert esd == sorted(esd)


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture object not present")
def test_normalize_implies_hex():
    """A comparison that ignored the code would call two unrelated modules
    equal, so --normalize turns the hex dump on whether or not -x was given."""
    hexLine = re.compile(r"^\s+[0-9A-F]+:\s+[0-9A-F]{4}")
    assert any(hexLine.match(l) for l in dump("--normalize"))
    assert not any(hexLine.match(l) for l in dump())


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture object not present")
def test_default_output_is_unaffected():
    """The switch must not change what everyone else already reads."""
    assert dump() == dump("--no-repro")


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture object not present")
def test_normalize_still_reports_every_record():
    """Sorting groups records; it must not drop any."""
    plain = dump("--hex")
    norm = dump("--normalize")
    for kind in ("ESD", "TXT", "RLD", "END"):
        assert (len([l for l in plain if l.startswith(kind)])
                == len([l for l in norm if l.startswith(kind)])), kind
