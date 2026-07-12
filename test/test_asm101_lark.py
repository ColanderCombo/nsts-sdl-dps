#!/usr/bin/env python3
"""Frozen-golden regression test for the Lark operand/expression parser
(asm101/larkparse.py + asm.lark).

The corpus is ~12.5k (text, rule) inputs -- the real fields BILDNEW5 and the
curated edge cases parse -- and the expectations are Lark's own frozen output
(its assembly of BILDNEW5 + the 6 MLIB80 decks is byte-identical-verified).  A
pure regression guard: any future change to the grammar or transformer that
alters a parse is caught here, in-tree, with no dependency on the out-of-tree
flight listings.

The golden (test/asm101/lark_golden.json) covers only rules Lark implements.

A small number of golden entries are "None": inputs Lark declines because it is
anchored + Stage-0-comment-stripped (e.g. an `arithmeticExpression` on a
comma-list, adjacent/dotted `&var` concatenation in arithmetic, or a standalone
space-containing `booleanExpression` normally only seen inside AIF parens).
They are output-irrelevant on the byte-identical-verified decks; freezing them
locks the current behaviour.

Run: PYTHONPATH=src python3 test/test_asm101_lark.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "asm101"))

from asm101 import larkparse                       # noqa: E402

HERE = Path(__file__).resolve().parent
GOLDEN_FILE = HERE / "asm101" / "lark_golden.json"


def _canon(x):
  """Canonical, tuple/list/dict-distinguishing, key-sorted string form."""
  if isinstance(x, dict):
    return "{" + ",".join("%r:%s" % (k, _canon(v))
                          for k, v in sorted(x.items())) + "}"
  if isinstance(x, tuple):
    return "T(" + ",".join(_canon(e) for e in x) + ")"
  if isinstance(x, list):
    return "[" + ",".join(_canon(e) for e in x) + "]"
  return repr(x)


def regen():
  """Recompute every entry's canonical form from the live parser and rewrite
  the golden in place (entry order and t/r fields preserved).  Use after an
  intentional grammar/transformer change -- e.g. typing an AST node -- then
  eyeball the diff to confirm only the intended entries moved.  Serialized to
  match the checked-in one-line form (default separators, no trailing NL)."""
  golden = json.load(open(GOLDEN_FILE))
  for entry in golden:
    entry["canon"] = _canon(larkparse.parse(entry["t"], entry["r"]))
  with open(GOLDEN_FILE, "w") as f:
    json.dump(golden, f)
  print("regenerated %d golden entries" % len(golden))
  return 0


def main():
  if "--regen" in sys.argv:
    return regen()
  if not GOLDEN_FILE.exists():
    print("FAIL: %s missing" % GOLDEN_FILE)
    return 1
  golden = json.load(open(GOLDEN_FILE))
  failures = []
  for entry in golden:
    text, rule, want = entry["t"], entry["r"], entry["canon"]
    got = _canon(larkparse.parse(text, rule))
    if got != want:
      failures.append((rule, text, want, got))
  for rule, text, want, got in failures[:25]:
    print("MISMATCH [%s] %r\n  golden: %s\n  lark:   %s" %
          (rule, text, want, got))
  print("checked=%d golden entries; mismatches=%d" % (len(golden), len(failures)))
  if failures:
    print("FAIL: Lark parser diverged from the frozen golden")
    return 1
  print("OK: Lark parser matches the frozen golden on all %d entries" % len(golden))
  return 0


if __name__ == "__main__":
  sys.exit(main())
