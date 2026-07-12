#!/usr/bin/env python3
#
# smoke test for the upstream ext/virtualagc/ASM101S assembler.
#
# Assembles a tiny fixture and compares the resulting .obj
# byte-for-byte against a checked-in expected file.
#
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ASM101S_SCRIPT = REPO_ROOT / "ext" / "virtualagc" / "ASM101S" / "ASM101S.py"
FIXTURE_ASM = (
    REPO_ROOT / "ext" / "virtualagc" / "yaShuttle" / "Source Code"
    / "PASS.REL32V0" / "ZCONASM" / "#QSIN.asm"
)
EXPECTED_OBJ = Path(__file__).parent / "data" / "asm101s" / "QSIN.expected.obj"


def main():
  # This smoke test exercises the UPSTREAM ext/virtualagc/ASM101S assembler,
  # which still depends on TatSu (our fork in src/asm101 no longer does).
  # Skip cleanly if TatSu is not installed rather than hard-failing.
  import importlib.util
  if importlib.util.find_spec("tatsu") is None:
    print("SKIP: upstream ASM101S smoke test needs `tatsu` installed "
          "(our src/asm101 fork is TatSu-free); `uv pip install tatsu` "
          "to run it.")
    return

  for p in (ASM101S_SCRIPT, FIXTURE_ASM, EXPECTED_OBJ):
    if not p.is_file():
      sys.exit(f"required input not found: {p}")

  with tempfile.TemporaryDirectory(prefix="asm101s_smoke_") as tmpdir:
    out_obj = Path(tmpdir) / "QSIN.obj"
    env = os.environ | {"PYTHONUTF8": "1"}
    result = subprocess.run(
        [
            sys.executable,
            str(ASM101S_SCRIPT),
            f"--object={out_obj}",
            str(FIXTURE_ASM),
        ],
        cwd=tmpdir,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
      sys.stderr.write(result.stdout)
      sys.stderr.write(result.stderr)
      sys.exit(f"ASM101S.py exited with {result.returncode}")
    if not out_obj.is_file():
      sys.exit(f"ASM101S.py did not produce {out_obj}")

    actual = out_obj.read_bytes()
    expected = EXPECTED_OBJ.read_bytes()
    if actual != expected:
      saved = REPO_ROOT / "build" / "QSIN.actual.obj"
      saved.parent.mkdir(parents=True, exist_ok=True)
      shutil.copy(out_obj, saved)
      sys.exit(
          "ASM101S output does not match expected.\n"
          f"  expected: {EXPECTED_OBJ} ({len(expected)} bytes)\n"
          f"  actual:   {saved} ({len(actual)} bytes)\n"
          f"Compare with: cmp {EXPECTED_OBJ} {saved}"
      )

  print(f"OK: ASM101S assembled {FIXTURE_ASM.name} matching expected {len(expected)}-byte .obj")


if __name__ == "__main__":
  main()
