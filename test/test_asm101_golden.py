#!/usr/bin/env python3
#
# Byte-golden regression for the src/asm101 assembler.
#
# Assembles every fixture under test/asm101/ with DEFAULT flags and byte-
# compares each one that produces an object against a checked-in golden in
# test/asm101/golden/.  This is the broad "did the emitted bytes move" net
# behind the property-level checks in test_asm101_features.py: a single
# instruction-encoding, ESD-ordering, RLD, or object-layout change anywhere
# trips exactly the affected golden(s).
#
# Contract (verify mode, the default):
#   * Every source that assembles cleanly (exit 0, object written) MUST have a
#     matching golden, and the bytes must match.  A new producing fixture with
#     no golden is a FAILURE -- run with --regen to mint it.
#   * A source that errors / needs a macro library (-L) / is a negative test
#     produces no object and is simply skipped (it is covered elsewhere).
#
# Regenerate after an intentional output change:
#     python test_asm101_golden.py --regen
# (or set GOLDEN_REGEN=1).  Regen writes goldens for all current producers and
# deletes stale goldens whose source no longer produces one.
#
# Run via ctest:  ctest -R asm101_golden       (registered in asm101/CMakeLists.txt)

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIX = HERE / "asm101"
GOLDEN = FIX / "golden"


def assemble(src: Path, obj: Path) -> tuple[int, str]:
  """Assemble `src` to `obj` with default flags; return (returncode, output)."""
  env = os.environ | {"PYTHONUTF8": "1"}
  r = subprocess.run(
      [sys.executable, "-m", "asm101", "-o", str(obj), str(src)],
      capture_output=True, text=True, env=env, timeout=120,
  )
  return r.returncode, (r.stdout + r.stderr)


def produced_objects(tmp: Path) -> dict[str, bytes]:
  """Assemble every fixture; return {stem: object-bytes} for the producers."""
  out = {}
  for src in sorted(FIX.glob("*.asm")):
    obj = tmp / (src.stem + ".obj")
    rc, _ = assemble(src, obj)
    if rc == 0 and obj.exists():
      out[src.stem] = obj.read_bytes()
  return out


def regen() -> int:
  GOLDEN.mkdir(exist_ok=True)
  with tempfile.TemporaryDirectory(prefix="asm101_golden_") as td:
    produced = produced_objects(Path(td))
  for stem, data in produced.items():
    (GOLDEN / f"{stem}.obj").write_bytes(data)
  stale = [g for g in GOLDEN.glob("*.obj") if g.stem not in produced]
  for g in stale:
    g.unlink()
  print(f"Regenerated {len(produced)} golden object(s)"
        + (f"; removed {len(stale)} stale" if stale else ""))
  return 0


def verify() -> int:
  if not GOLDEN.is_dir() or not any(GOLDEN.glob("*.obj")):
    print(f"FAILED: no goldens in {GOLDEN}; run with --regen", file=sys.stderr)
    return 1
  with tempfile.TemporaryDirectory(prefix="asm101_golden_") as td:
    produced = produced_objects(Path(td))

  failures = []
  for stem, data in sorted(produced.items()):
    golden = GOLDEN / f"{stem}.obj"
    if not golden.exists():
      failures.append(f"{stem}: produces an object but has no golden "
                      f"(run --regen)")
    elif golden.read_bytes() != data:
      failures.append(f"{stem}: object bytes differ from golden "
                      f"({len(data)} vs {len(golden.read_bytes())} bytes)")
  orphans = [g.stem for g in GOLDEN.glob("*.obj") if g.stem not in produced]
  for stem in sorted(orphans):
    failures.append(f"{stem}: golden exists but source no longer produces "
                    f"an object (delete it or run --regen)")

  if failures:
    print(f"FAILED {len(failures)} asm101 golden check(s):", file=sys.stderr)
    for f in failures:
      print(f"  - {f}", file=sys.stderr)
    return 1
  print(f"OK: all {len(produced)} asm101 object goldens match")
  return 0


if __name__ == "__main__":
  if "--regen" in sys.argv[1:] or os.environ.get("GOLDEN_REGEN") == "1":
    raise SystemExit(regen())
  raise SystemExit(verify())
