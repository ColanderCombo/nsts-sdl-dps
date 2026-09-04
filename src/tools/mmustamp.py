#!/usr/bin/env python3
"""mmustamp -- write the Mass-Memory-Build tables into the phase load modules.

Three csects hold Mass Memory Build content; the linkedit image reads
zeros (or -1) where they sit:

  #PFCMGPT  the in-core phase table: for every phase, its load blocks and
            the mass memory address of the first one
  #PCDCPHA  the same addresses for the alternate MM areas
  FCMG3DAT  the G3 archive destination address table

`ap101Utils.mmbstamp.generate` derives all three from the linked phases
and the CON80 MMU cards, and `stamp_libs` writes them into the modules.
`tools.mmu2mmv` builds each tape record out of the load module's text.
`mmu2fcm --stamp-phase-tables` stamps a composed image and leaves the
module clean.

It mutates the linkedit output and runs on its own.  What each module
carried goes to a sidecar beside it (`PHASEnn.mmbstamp.json`), `--restore`
puts it back, and stamping twice is a no-op.  Re-run it after a relink:
the tables are a readout of where the linker put every csect.

Usage:
  mmustamp --mmu build/OI340700 --con80 code/OI340700/CON80
  mmustamp --mmu build/OI340700 --con80 code/OI340700/CON80 --report
  mmustamp --mmu build/OI340700 --restore
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from ap101Utils import mmbstamp

log = logging.getLogger("mmustamp")


def report(stamps, out=sys.stdout) -> None:
    if not stamps:
        print("nothing stamped", file=out)
        return
    print(f"{'phase':>5}  {'csect':9} {'address':>8} {'hw':>5}  "
          f"{'was':>6}  {'now':>6}", file=out)
    for s in stamps:
        was = sum(1 for w in s.before if w)
        now = sum(1 for w in s.after if w)
        print(f"{s.phase:5d}  {s.csect:9} {s.address:#08x} {len(s.after):5d}"
              f"  {was:6d}  {now:6d}"
              f"{'' if s.changed else '   (unchanged)'}", file=out)
    print("\n'was'/'now' count the halfwords that are not zero.", file=out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="mmustamp",
        description="Write the Mass-Memory-Build tables (#PFCMGPT, "
                    "#PCDCPHA, FCMG3DAT) into the phase load modules that "
                    "carry them")
    ap.add_argument("--mmu", default="build/mmu", type=Path,
                    help="con80build output root holding PHASEnn.lib "
                         "(default: build/mmu)")
    ap.add_argument("--con80", default="code/OI340700/CON80", type=Path,
                    help="linkedit/MMU card deck (default: "
                         "code/OI340700/CON80)")
    ap.add_argument("--restore", action="store_true",
                    help="put the linkedit content back from the sidecars "
                         "and drop them")
    ap.add_argument("--report", action="store_true",
                    help="say what would be written and change nothing")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(levelname)s: %(message)s")

    if not a.mmu.is_dir():
        ap.error(f"{a.mmu}: phase tree not found")

    try:
        if a.restore:
            stamps = mmbstamp.restore_libs(a.mmu, dry_run=a.report)
            report(stamps)
            if stamps and not a.report:
                log.info("restored %d table(s) in %d module(s)", len(stamps),
                         len({s.phase for s in stamps}))
            return 0

        if not a.con80.is_dir():
            ap.error(f"{a.con80}: card deck not found")
        tables = mmbstamp.generate(a.mmu, a.con80)
        for n in tables.notes:
            log.debug("mmbstamp: %s", n)
        stamps = mmbstamp.stamp_libs(a.mmu, tables, dry_run=a.report)
    except mmbstamp.StampError as e:
        ap.error(str(e))

    report(stamps)
    if not a.report:
        log.info("stamped %d table(s) into %d module(s) -- Mass-Memory-"
                 "Build content; --restore undoes it", len(stamps),
                 len({s.phase for s in stamps}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
