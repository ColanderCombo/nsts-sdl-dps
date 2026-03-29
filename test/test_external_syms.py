#!/usr/bin/env python3
#
# Test --external-syms: single-module relocation matches full link.
#
# For a given program that was fully linked (with all libraries), this test:
#   1. Uses the .ext.json produced by the full link (--save-external-syms)
#   2. Runs lnk101 on just the main .obj with --external-syms
#   3. Compares section bytes using fcmcmp
#
# Usage:
#     python test_external_syms.py <program_name> <build_dir> <venv_python>
#

import subprocess
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <program_name> <build_dir> <venv_python>",
              file=sys.stderr)
        sys.exit(2)

    program = sys.argv[1]
    build_dir = Path(sys.argv[2])
    venv_python = sys.argv[3]

    # Full-link artifacts
    full_fcm = build_dir / "fcm" / f"{program}.fcm"
    full_ext = build_dir / "fcm" / f"{program}.ext.json"

    for p in (full_fcm, full_ext):
        if not p.exists():
            print(f"SKIP: {p} not found (build incomplete?)", file=sys.stderr)
            sys.exit(0)

    # Find the main .obj
    main_obj = None
    for subdir in ("bench", "progHal"):
        candidate = build_dir / subdir / f"{program}.obj"
        if candidate.exists():
            main_obj = candidate
            break
    if main_obj is None:
        print(f"SKIP: {program}.obj not found", file=sys.stderr)
        sys.exit(0)

    import tempfile
    with tempfile.TemporaryDirectory(prefix="extsym_test_") as tmpdir:
        tmpdir = Path(tmpdir)

        # Single-module link using the build's .ext.json
        single_fcm = tmpdir / f"{program}.fcm"
        single_sym = tmpdir / f"{program}.sym.json"
        result = subprocess.run([
            venv_python, "-m", "lnk101",
            str(main_obj),
            "-o", str(single_fcm),
            "--external-syms", str(full_ext),
            "--json-symbols", str(single_sym),
        ], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"FAIL: single-module link failed:\n{result.stderr}",
                  file=sys.stderr)
            sys.exit(1)

        # Compare using fcmcmp (auto-detects real modules from sym.json)
        result = subprocess.run([
            venv_python, "-m", "lnk101.fcmcmp",
            str(single_sym), str(full_fcm), str(single_fcm),
        ], capture_output=True, text=True)
        print(result.stdout, end="")
        if result.returncode != 0:
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
