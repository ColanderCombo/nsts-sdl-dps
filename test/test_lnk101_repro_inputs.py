#!/usr/bin/env python3
"""A reproducibility record has to name the inputs that decide the output.

Two links of "the same inputs" once disagreed by 177 csect addresses, and the
saved `repro` blocks of the two runs were identical -- because the block
listed only the object files.  The deciding input was elsewhere: the
earlier-phase load modules handed to the link, and the overlay-region origins
imported from them.  An origin-less OVERLAY card naming a region an earlier
phase defines RE-OPENS that region at the recorded origin; the same card with
no such module supplied is instead a NEW overlay, and the layout differs.

So the record must carry, beyond the object files:

  * the `--map-lib` set, and each of those files' checksums;
  * the region origins the link actually imported from them;
  * the concard root the layout was scoped to.

The second contract here is the timestamp.  A load module carries a `created`
stamp, and it was the only reason a repeat link could not be compared with
`cmp`.  Under SOURCE_DATE_EPOCH it is pinned, so byte-identity is testable;
with the variable unset the behaviour is the wall clock, unchanged.

Everything below is synthetic.
"""
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnk101.repro import ReproTracker, created_stamp     # noqa: E402

failures = []


def check(cond, what):
    if cond:
        print(f"  ok   {what}")
    else:
        failures.append(what)
        print(f"  FAIL {what}")


print("repro record: non-file inputs")

t = ReproTracker("lnk101", "test")
check("inputs" not in t.to_dict(),
      "a record with no non-file inputs carries no 'inputs' key")

t.inputs["mapLibs"] = ["2=/x/PHASE02.lib", "15=/x/PHASE15.lib"]
t.inputs["phaseRegionOrigins"] = {"AAAAAAAA": 0x1234, "BBBBBBBB": 0x5678}
t.inputs["concardRoot"] = "DECK@9"
d = t.to_dict()
check(d.get("inputs", {}).get("mapLibs") == ["2=/x/PHASE02.lib",
                                             "15=/x/PHASE15.lib"],
      "the map-lib set is recorded")
check(d["inputs"]["phaseRegionOrigins"] == {"AAAAAAAA": 0x1234,
                                            "BBBBBBBB": 0x5678},
      "the imported region origins are recorded")
check(d["inputs"]["concardRoot"] == "DECK@9",
      "the concard root is recorded")

# Two runs that differ ONLY in a non-file input must not look identical.
u = ReproTracker("lnk101", "test")
u.inputs["mapLibs"] = ["2=/x/PHASE02.lib"]
u.inputs["phaseRegionOrigins"] = {"AAAAAAAA": 0x1234}
u.inputs["concardRoot"] = "DECK@9"
check(u.to_dict() != d,
      "dropping an earlier-phase module makes the two records differ")

print("load-module timestamp")

os.environ["SOURCE_DATE_EPOCH"] = "1000000000"
try:
    a, b = created_stamp(), created_stamp()
    check(a == b, "SOURCE_DATE_EPOCH pins the stamp (two calls agree)")
    check(a == "2001-09-09T01:46:40",
          "the pinned stamp is that instant in UTC, second resolution")
    os.environ["SOURCE_DATE_EPOCH"] = "  1000000000  "
    check(created_stamp() == a, "surrounding whitespace is tolerated")
    os.environ["SOURCE_DATE_EPOCH"] = "not-a-number"
    now = created_stamp()
    check(now != a, "a non-numeric value falls back to the clock")
    datetime.fromisoformat(now)          # raises if malformed
finally:
    os.environ.pop("SOURCE_DATE_EPOCH", None)

live = created_stamp()
datetime.fromisoformat(live)
check(live != "2001-09-09T01:46:40",
      "with the variable unset the stamp is the wall clock, as before")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("OK: all repro-input and timestamp checks passed")
