#!/usr/bin/env python3
"""Frozen-golden regression test for cardreader.joinOperand.

joinOperand merges a macro prototype/invocation operand field across IBM
80-column continuation cards (column-72 flag + continuation-card discrimination,
comment trimming).  The fixtures are real Space Shuttle FSW macro decks; the
golden (test/asm101/joinoperand_golden.json) records, for each, the expected
(status, merged-operand, continuation-line count).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "asm101"))

from asm101.cardreader import joinOperand             # noqa: E402

GOLDEN_FILE = Path(__file__).resolve().parent / "asm101" / "joinoperand_golden.json"


def main():
  if not GOLDEN_FILE.exists():
    print("FAIL: %s missing" % GOLDEN_FILE)
    return 1
  golden = json.load(open(GOLDEN_FILE))
  failures = []
  for case in golden:
    kw = {case["kind"]: True}
    got = list(joinOperand(case["lines"], case["index"], case["column"], **kw))
    if got != case["expected"]:
      failures.append((case["kind"], case["column"], case["expected"], got))
  for kind, col, want, got in failures:
    print("MISMATCH [%s col%d]\n  golden: %s\n  got:    %s" % (kind, col, want, got))
  print("checked=%d joinOperand cases; mismatches=%d" % (len(golden), len(failures)))
  if failures:
    print("FAIL: joinOperand diverged from the frozen golden")
    return 1
  print("OK: joinOperand matches the frozen golden on all %d cases" % len(golden))
  return 0


if __name__ == "__main__":
  sys.exit(main())
