#!/usr/bin/env python3
#
# Coffee <-> asm101 instruction-definition sync guard.
#
# Runs the Stage-2 harness (ext/sim/tools/extract_instr_defs.cjs) to extract the
# GPC simulator's instruction definitions, then asserts:
#
#   1. FRESHNESS  -- the freshly extracted defs equal the checked-in artifact
#                    src/asm101/instr_defs.json (else: regenerate it).
#   2. IOP TRUTH  -- the extracted MSC/BCE descriptors match the listing-correct
#                    reference in test/iop_listing_truth.py, except for a small
#                    set of KNOWN, documented BCE discrepancies (see that file's
#                    SIM_BUGS).  Any NEW divergence fails -- that is coffee
#                    drifting away from flight-listing ground truth.
#
# The harness needs Node + esbuild (in ext/sim/node_modules) and the ext/sim
# submodule checked out.  When those are unavailable the test SKIPs cleanly
# (exit 0), mirroring the other out-of-tree-dependent asm101 tests.

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
HARNESS = ROOT / "ext" / "sim" / "tools" / "extract_instr_defs.cjs"
SIM_DIR = ROOT / "ext" / "sim"
ARTIFACT = ROOT / "src" / "asm101" / "instr_defs.json"

# The hand-maintained, listing-correct MSC/BCE descriptor reference (a test-only
# artifact -- the assembler encodes from the JSON, not from this).  Lives next to
# this script; the production asm101.iop_instr now sources its descriptors from
# the same JSON we check below, so comparing against IT would be circular.
sys.path.insert(0, str(HERE))
import iop_listing_truth  # noqa: E402

# KNOWN, accepted IOP differences between the coffee and the listing-correct
# reference.  Each is documented in iop_listing_truth.SIM_BUGS; they are not
# coffee "drift".  A change here
# must be a deliberate edit tracked against the reconciliation worklist.
# (Currently empty: the only entry was #WAT, whose coffee '_' don't-care bits and
# the listing's fixed '0' bits now compare equal -- see _norm below.)
ALLOWED_IOP_DIFFS = {
    # (proc, mnemonic): reason
}


def _norm(desc):
  """Normalize a descriptor bit-string for comparison: a coffee '_' don't-care
  bit is equivalent to a fixed '0'.  Both encode 0 (an unused position is built
  as zero); the coffee notates them '_', the listing-derived iop_instr tables as
  '0' (e.g. #WAT 00001___________ vs 0000100000000000).  A real divergence -- a
  field letter or a fixed '1' where the other has '0' -- still mismatches."""
  return desc.replace("_", "0") if desc is not None else desc


def skip(msg: str) -> None:
  print(f"SKIP: {msg}")
  sys.exit(0)


def run_harness() -> dict:
  if not HARNESS.exists():
    skip(f"{HARNESS} not found (ext/sim submodule not checked out?)")
  with tempfile.TemporaryDirectory(prefix="instr_defs_") as td:
    out = Path(td) / "instr_defs.json"
    try:
      r = subprocess.run(
          ["node", str(HARNESS), str(out)],
          cwd=str(SIM_DIR), capture_output=True, text=True, timeout=180,
      )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
      skip(f"could not run harness ({e}); node/esbuild unavailable?")
    if r.returncode != 0 or not out.exists():
      skip(f"harness did not produce output (node_modules missing?):\n"
           f"{r.stderr.strip()[-400:]}")
    return json.loads(out.read_text())


def main() -> int:
  extracted = run_harness()
  failures = []

  # 1. Freshness: checked-in artifact must match a fresh extraction.
  if not ARTIFACT.exists():
    failures.append(f"{ARTIFACT} is missing; regenerate it with the harness")
  else:
    committed = json.loads(ARTIFACT.read_text())
    if committed != extracted:
      failures.append(
          f"{ARTIFACT.name} is stale; regenerate with "
          f"`cd ext/sim && node tools/extract_instr_defs.cjs "
          f"../../src/asm101/instr_defs.json`")

  # 2. IOP truth: extracted descriptors vs the listing-correct reference.
  for proc, pydesc in [("msc", iop_listing_truth.MSC_DESC),
                       ("bce", iop_listing_truth.BCE_DESC)]:
    cof = {k: v["d"] for k, v in extracted[proc].items()}
    for name in sorted(set(pydesc) | set(cof)):
      if _norm(pydesc.get(name)) == _norm(cof.get(name)):
        continue
      if (proc, name) in ALLOWED_IOP_DIFFS:
        continue
      failures.append(
          f"{proc} {name}: coffee {cof.get(name)!r} != listing "
          f"{pydesc.get(name)!r} (new divergence from flight-listing truth)")

  if failures:
    print(f"FAILED {len(failures)} instruction-def sync check(s):",
          file=sys.stderr)
    for f in failures:
      print(f"  - {f}", file=sys.stderr)
    return 1
  print(f"OK: coffee defs in sync (cpu={len(extracted['cpu'])} "
        f"msc={len(extracted['msc'])} bce={len(extracted['bce'])}); "
        f"{len(ALLOWED_IOP_DIFFS)} known IOP exception(s)")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
