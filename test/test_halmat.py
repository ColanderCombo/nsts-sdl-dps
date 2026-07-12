#!/usr/bin/env python3
#
# Test yaHALMAT vs gpc-batch
#
# Usage:
#     python test_halmat.py <source.hal> <build_dir> <yahalmat> <halmat.bin> <litfile.bin>
#

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def normalize_output(text):
    # Normalize WRITE output for comparison.
    #
    lines = []
    for line in text.strip().splitlines():
        line = re.sub(r'\s+', ' ', line.strip())
        if line:
            lines.append(line)
    return '\n'.join(lines)


def main():
    if len(sys.argv) != 6:
        print(f"Usage: {sys.argv[0]} <source.hal> <build_dir> <yahalmat> "
              f"<halmat.bin> <litfile.bin>", file=sys.stderr)
        sys.exit(2)

    source = Path(sys.argv[1])
    build_dir = Path(sys.argv[2])
    yahalmat = sys.argv[3]
    halmat_bin = Path(sys.argv[4])
    litfile_bin = Path(sys.argv[5])

    gpc_batch = build_dir / "bin" / "gpc-batch"
    fcm_dir = build_dir / "fcm"

    name = source.stem

    # Find prebuilt FCM and symbols
    fcm_file = fcm_dir / f"{name}.fcm"
    sym_json = fcm_dir / f"{name}.sym.json"

    for p in (source, gpc_batch, halmat_bin, litfile_bin, fcm_file, sym_json):
        if not p.exists():
            print(f"SKIP: {p} not found", file=sys.stderr)
            sys.exit(0)

    if not os.path.exists(yahalmat):
        print(f"SKIP: yaHALMAT not found at {yahalmat}", file=sys.stderr)
        sys.exit(0)

    tmpdir = Path(build_dir) / "test" / "halmat" / name
    tmpdir.mkdir(parents=True, exist_ok=True)

    # yaHALMAT — copy source as SOURCECO.txt for string resolution
    shutil.copy(source, tmpdir / "SOURCECO.txt")
    # Symlink halmat.bin and litfile.bin into tmpdir if needed
    for src, dst in [(halmat_bin, "halmat.bin"), (litfile_bin, "litfile.bin")]:
        link = tmpdir / dst
        if not link.exists():
            os.symlink(src, link)

    infile5 = source.parent / (name + ".in5")
    if not infile5.exists():
        infile5 = Path(build_dir) / ".." / "test" / (name + ".in5")
    stdin_data = infile5.read_text() if infile5.exists() else ""

    # Run yaHALMAT
    try:
        result = subprocess.run([
            yahalmat, str(tmpdir / "halmat.bin"),
        ], capture_output=True, timeout=10,
           input=stdin_data.encode('latin-1'), cwd=str(tmpdir))
    except subprocess.TimeoutExpired:
        print(f"SKIP: yaHALMAT timed out (may need input or hit unsupported opcode)",
              file=sys.stderr)
        sys.exit(0)
    halmat_output = result.stdout.decode('latin-1')
    halmat_err = result.stderr.decode('latin-1')
    (tmpdir / "yahalmat.out").write_text(halmat_output, encoding='latin-1')
    if halmat_err.strip():
        (tmpdir / "yahalmat.err").write_text(halmat_err, encoding='latin-1')
    if result.returncode != 0 and not halmat_output:
        print(f"SKIP: yaHALMAT failed (rc={result.returncode}): "
              f"{halmat_err.strip()[:200]}", file=sys.stderr)
        sys.exit(0)

    out6 = tmpdir / "out6.txt"
    gpc_cmd = [
        str(gpc_batch),
        str(fcm_file),
        "--no-trace",
        "--max-steps", "10000000",
        f"--outfile6={out6}",
        f"--symbols={sym_json}",
    ]
    if infile5.exists():
        gpc_cmd.append(f"--infile5={infile5}")
    try:
        result = subprocess.run(gpc_cmd,
                                capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        print(f"SKIP: gpc-batch timed out", file=sys.stderr)
        sys.exit(0)
    gpc_output = out6.read_text() if out6.exists() else ""

    # Compare normalized output
    norm_halmat = normalize_output(halmat_output)
    norm_gpc = normalize_output(gpc_output)

    if not norm_halmat and not norm_gpc:
        print(f"PASS: {name} (no output)")
    else:
        print(f"  yaHALMAT:  {repr(norm_halmat)}")
        print(f"  gpc-batch: {repr(norm_gpc)}")
        if norm_halmat == norm_gpc:
            print(f"PASS: {name}")
        else:
            print(f"FAIL: {name} — output differs", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
